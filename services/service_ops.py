"""Admin operations on a live service: extend, add data, disable, enable.

The PasarGuard client has had ``modify_user`` since phase 1, but no sales path
ever called it — the only way to give a customer a few extra days after an
outage was to open the panel itself. These are the four things an operator
actually needs mid-support-conversation.

Writing an absolute value to the panel is necessary and not sufficient. A
retry that re-reads the already-extended snapshot and recomputes will still
double-apply. The durable fix matches ``_provision_renew``: compute the
absolute target, commit it on a ``service_operations`` row **before** the
panel call, and on re-entry re-apply that stored target instead of reading
the panel again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.client import PasarGuardClient
from api_client.exceptions import PasarGuardError
from api_client.models import UserHWIDResponse
from database.base import end_read_transaction
from database.models.order import Order, OrderStatus
from database.models.pasarguard_account import PasarGuardAccount
from database.models.service_operation import (
    KIND_ADD_DATA,
    KIND_EXTEND_DAYS,
    STATE_APPLIED,
    STATE_PENDING,
    STATE_SUPERSEDED,
    ServiceOperation,
)
from services.accounts import get_live_account, live_account_clause
from services.provisioning import compute_renew_expire_unix, expire_to_unix

logger = logging.getLogger(__name__)

__all__ = [
    "ServiceOpError",
    "ServiceSnapshot",
    "MAX_MANUAL_DAYS",
    "MAX_MANUAL_GB",
    "add_data",
    "SupportExtras",
    "extend_days",
    "get_snapshot",
    "get_support_extras",
    "list_devices",
    "reset_devices",
    "revoke_link",
    "set_enabled",
    "set_user_services_enabled",
]

_BYTES_PER_GB = 1024**3

# Sanity rails for a hand-typed number, matching the catalog's DIM_LIMITS.
MAX_MANUAL_DAYS = 3_650
MAX_MANUAL_GB = 10_000


class ServiceOpError(Exception):
    """A manual service change that must not be applied, in Persian."""


@dataclass(frozen=True)
class ServiceSnapshot:
    """What the panel says about one service right now."""

    panel_username: str
    status: str | None
    expire_unix: int | None
    data_limit_bytes: int | None
    used_traffic_bytes: int
    replayed: bool = False
    # Support extras, filled by get_snapshot from the same UserResponse; the
    # defaults keep every older constructor working.
    online_at: datetime | None = None
    hwid_limit: int | None = None
    panel_user_id: int | None = None

    @property
    def expire_at(self) -> datetime | None:
        if self.expire_unix is None:
            return None
        return datetime.fromtimestamp(self.expire_unix, tz=UTC)

    @property
    def is_unlimited_data(self) -> bool:
        return self.data_limit_bytes is None or self.data_limit_bytes <= 0

    @property
    def remaining_bytes(self) -> int | None:
        if self.is_unlimited_data:
            return None
        return max(0, int(self.data_limit_bytes or 0) - self.used_traffic_bytes)


async def _account(session: AsyncSession, account_id: int) -> PasarGuardAccount:
    account = await get_live_account(session, account_id)
    if account is None:
        raise ServiceOpError("سرویس یافت نشد.")
    return account


async def get_snapshot(
    session: AsyncSession, client: PasarGuardClient, *, account_id: int
) -> ServiceSnapshot:
    """Read the live panel state. The panel is the source of truth, not the DB."""
    account = await _account(session, account_id)
    try:
        user = await client.get_user(str(account.panel_username))
    except PasarGuardError as exc:
        raise ServiceOpError(f"پنل پاسخ نداد: {exc}") from exc
    return ServiceSnapshot(
        panel_username=str(account.panel_username),
        status=user.status.value if user.status is not None else None,
        expire_unix=expire_to_unix(user.expire),
        data_limit_bytes=user.data_limit,
        used_traffic_bytes=int(getattr(user, "used_traffic", 0) or 0),
        online_at=getattr(user, "online_at", None),
        hwid_limit=getattr(user, "hwid_limit", None),
        panel_user_id=int(account.panel_user_id),
    )


async def _operation_for_caller(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    operation_key: str | None,
) -> tuple[ServiceOperation | None, bool]:
    """Find the row this caller already started.

    A matching applied row is a replay: the panel was already moved, so a
    second call must not compute a new target. Anything else falls back to
    the single pending row for this account and kind.
    """
    if operation_key:
        found = await session.scalar(
            select(ServiceOperation).where(ServiceOperation.operation_key == operation_key)
        )
        if found is not None:
            if int(found.account_id) != account_id or found.kind != kind:
                raise ServiceOpError("این درخواست برای سرویس دیگری است.")
            return found, found.state == STATE_APPLIED
    return await _pending_op(session, account_id=account_id, kind=kind), False


async def _pending_op(
    session: AsyncSession, *, account_id: int, kind: str
) -> ServiceOperation | None:
    return await session.scalar(
        select(ServiceOperation).where(
            ServiceOperation.account_id == account_id,
            ServiceOperation.kind == kind,
            ServiceOperation.state == STATE_PENDING,
        )
    )


async def _drop_if_stale(
    session: AsyncSession, op: ServiceOperation | None, *, requested: int
) -> ServiceOperation | None:
    """A pending row is a retry of ONE request. A different amount is a new one.

    Re-applying the stored target for another amount is how a failed 7-day tap
    answered the next 30-day tap with the old expiry. Rows from before the
    amount was stored (NULL) cannot be compared and keep the retry behaviour.
    """
    if op is None or op.state != STATE_PENDING:
        return op
    if op.requested_amount is None or int(op.requested_amount) == requested:
        return op
    op.state = STATE_SUPERSEDED
    await session.commit()
    return None


async def _mark_applied(session: AsyncSession, op: ServiceOperation) -> None:
    op.state = STATE_APPLIED
    op.applied_at = datetime.now(UTC)
    await session.commit()


async def _live_snapshot_for_new_target(
    session: AsyncSession, client: PasarGuardClient, *, account_id: int, kind: str
) -> ServiceSnapshot:
    """The panel read a new operation computes its target from.

    The lookup for a pending row has pinned a WAL read snapshot. Held across
    the panel read, any scheduler commit meanwhile makes the INSERT of the new
    row fail at once («database is locked», see
    ``database.base.end_read_transaction``): the admin got «یه مشکلی پیش اومد»
    for a tap that had changed nothing. So the snapshot is released first.

    That stale snapshot was also what stopped two operators from each
    computing a target off the same live expiry. The pending row is therefore
    looked up again after the read, in the transaction that inserts.
    """
    await end_read_transaction(session)
    snapshot = await get_snapshot(session, client, account_id=account_id)
    if await _pending_op(session, account_id=account_id, kind=kind) is not None:
        raise ServiceOpError("همین حالا تغییر دیگری روی این سرویس در جریان است. دوباره تلاش کن.")
    return snapshot


async def extend_days(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    account_id: int,
    days: int,
    now: datetime | None = None,
    operation_key: str | None = None,
) -> ServiceSnapshot:
    """Push the expiry out by ``days``, counting from whichever is later:
    the current expiry or now. Extending an already-expired service therefore
    gives a full fresh window rather than days that are already gone.

    The absolute target is committed on a pending ``service_operations`` row
    before the panel is touched; a retry re-applies that row instead of
    recomputing from a live (already extended) snapshot.
    """
    if days <= 0 or days > MAX_MANUAL_DAYS:
        raise ServiceOpError(f"تعداد روز باید بین ۱ و {MAX_MANUAL_DAYS} باشد.")

    account = await _account(session, account_id)
    op, replayed = await _operation_for_caller(
        session,
        account_id=account_id,
        kind=KIND_EXTEND_DAYS,
        operation_key=operation_key,
    )
    if replayed:
        snapshot = await get_snapshot(session, client, account_id=account_id)
        return replace(snapshot, replayed=True)
    op = await _drop_if_stale(session, op, requested=days)
    if op is None:
        snapshot = await _live_snapshot_for_new_target(
            session, client, account_id=account_id, kind=KIND_EXTEND_DAYS
        )
        if snapshot.expire_unix is None:
            raise ServiceOpError("این سرویس تاریخ انقضا ندارد؛ تمدید روز معنا ندارد.")
        new_expire = compute_renew_expire_unix(
            current_expire=snapshot.expire_unix, duration_days=days, now=now
        )
        op = ServiceOperation(
            account_id=account_id,
            kind=KIND_EXTEND_DAYS,
            target_expire_unix=int(new_expire),
            target_data_limit=None,
            requested_amount=days,
            state=STATE_PENDING,
            operation_key=operation_key,
        )
        session.add(op)
        # Commit, not flush: a flush dies with the caller's rollback (SH-01).
        await session.commit()
    else:
        new_expire = int(op.target_expire_unix or 0)

    try:
        await client.extend_subscription(str(account.panel_username), expire=new_expire)
    except PasarGuardError as exc:
        # Leave the row pending — the next attempt is a retry of this intent.
        raise ServiceOpError(f"تمدید انجام نشد: {exc}") from exc

    # The bot's own copy of the expiry must follow the panel. Renewal reminders
    # and the expiring filter read ``orders.expires_at``; left alone, a manual
    # extension made the reminder fire on the old date. Absolute value, so a
    # retry re-applies the same thing; committed together with the applied mark.
    await session.execute(
        update(Order)
        .where(
            Order.panel_username == str(account.panel_username),
            Order.status == OrderStatus.ACTIVE,
            Order.expires_at < datetime.fromtimestamp(int(new_expire), tz=UTC),
        )
        .values(expires_at=datetime.fromtimestamp(int(new_expire), tz=UTC))
    )
    await _clear_lifecycle_marks(session, account_id)
    await _mark_applied(session, op)
    return await get_snapshot(session, client, account_id=account_id)


async def _clear_lifecycle_marks(session: AsyncSession, account_id: int) -> None:
    """An admin changed the service: the lifecycle job's clock and intent no longer apply."""
    await session.execute(
        update(PasarGuardAccount)
        .where(PasarGuardAccount.id == account_id)
        .values(limited_since=None, delete_requested_at=None)
    )


async def add_data(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    account_id: int,
    gigabytes: int,
    operation_key: str | None = None,
) -> ServiceSnapshot:
    """Raise the data cap by ``gigabytes``.

    Refused on an unlimited service: there is no cap to raise, and writing one
    would quietly turn an unlimited plan into a metered one.

    Same durable-target pattern as ``extend_days``: commit the absolute limit
    before the panel call; retries re-apply the stored target.
    """
    if gigabytes <= 0 or gigabytes > MAX_MANUAL_GB:
        raise ServiceOpError(f"حجم باید بین ۱ و {MAX_MANUAL_GB} گیگ باشد.")

    account = await _account(session, account_id)
    op, replayed = await _operation_for_caller(
        session,
        account_id=account_id,
        kind=KIND_ADD_DATA,
        operation_key=operation_key,
    )
    if replayed:
        snapshot = await get_snapshot(session, client, account_id=account_id)
        return replace(snapshot, replayed=True)
    op = await _drop_if_stale(session, op, requested=gigabytes)
    if op is None:
        snapshot = await _live_snapshot_for_new_target(
            session, client, account_id=account_id, kind=KIND_ADD_DATA
        )
        if snapshot.is_unlimited_data:
            raise ServiceOpError("این سرویس نامحدود است؛ افزودن حجم معنا ندارد.")
        new_limit = int(snapshot.data_limit_bytes or 0) + gigabytes * _BYTES_PER_GB
        op = ServiceOperation(
            account_id=account_id,
            kind=KIND_ADD_DATA,
            target_expire_unix=None,
            target_data_limit=int(new_limit),
            requested_amount=gigabytes,
            state=STATE_PENDING,
            operation_key=operation_key,
        )
        session.add(op)
        await session.commit()
    else:
        new_limit = int(op.target_data_limit or 0)

    try:
        await client.increase_data_limit(str(account.panel_username), new_limit)
    except PasarGuardError as exc:
        raise ServiceOpError(f"افزودن حجم انجام نشد: {exc}") from exc

    await _clear_lifecycle_marks(session, account_id)
    await _mark_applied(session, op)
    return await get_snapshot(session, client, account_id=account_id)


async def set_enabled(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    account_id: int,
    enabled: bool,
) -> ServiceSnapshot:
    """Cut a service off, or restore it.

    Disable rather than delete: the customer's usage, expiry and subscription
    link survive, so an accidental cut-off is one tap to undo. Deleting a panel
    user is not offered here at all.
    """
    account = await _account(session, account_id)
    try:
        await client.disable_user(str(account.panel_username), disabled=not enabled)
    except PasarGuardError as exc:
        verb = "فعال‌سازی" if enabled else "قطع"
        raise ServiceOpError(f"{verb} سرویس انجام نشد: {exc}") from exc
    return await get_snapshot(session, client, account_id=account_id)


async def set_user_services_enabled(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    user_id: int,
    enabled: bool,
) -> tuple[int, int]:
    """Cut off or restore every service a customer owns; returns (done, failed).

    One panel failure never stops the rest: the owner would rather see «۲ از ۳»
    than a half-banned customer and an error.
    """
    accounts = (
        (
            await session.execute(
                select(PasarGuardAccount.id)
                .where(PasarGuardAccount.user_id == user_id, live_account_clause())
                .order_by(PasarGuardAccount.id)
            )
        )
        .scalars()
        .all()
    )
    done = failed = 0
    for account_id in accounts:
        try:
            await set_enabled(session, client, account_id=int(account_id), enabled=enabled)
            done += 1
        except ServiceOpError:
            failed += 1
    return done, failed


@dataclass(frozen=True)
class SupportExtras:
    """Decoration for the service card. Every field is None when its read failed."""

    device_count: int | None = None
    last_update_app: str | None = None
    last_update_at: datetime | None = None


def _app_name(user_agent: str) -> str:
    """«v2rayNG/1.8.5» -> «v2rayNG», at most 24 characters."""
    return user_agent.split("/", 1)[0].strip()[:24]


async def get_support_extras(client: PasarGuardClient, snapshot: ServiceSnapshot) -> SupportExtras:
    """Two extra panel reads that only decorate the card.

    Best-effort by design: a failure of either leaves that field None and the
    card omits the line. A slow extra must never take the whole screen down.
    """
    device_count: int | None = None
    app: str | None = None
    at: datetime | None = None
    if snapshot.panel_user_id is not None:
        try:
            device_count = int((await client.get_user_hwids(snapshot.panel_user_id)).count)
        except Exception:  # noqa: BLE001 - decoration only
            logger.warning("hwid read failed for %s", snapshot.panel_username)
    try:
        updates = (await client.list_sub_updates(snapshot.panel_username, limit=1)).updates
        if updates:
            app = _app_name(updates[0].user_agent) or None
            at = updates[0].created_at
    except Exception:  # noqa: BLE001 - decoration only
        logger.warning("sub_update read failed for %s", snapshot.panel_username)
    return SupportExtras(device_count=device_count, last_update_app=app, last_update_at=at)


async def list_devices(
    session: AsyncSession, client: PasarGuardClient, *, account_id: int
) -> list[UserHWIDResponse]:
    """The devices registered on this service (a real read: the screen is about it)."""
    account = await _account(session, account_id)
    try:
        result = await client.get_user_hwids(int(account.panel_user_id))
    except PasarGuardError as exc:
        raise ServiceOpError(f"پنل پاسخ نداد: {exc}") from exc
    return list(result.hwids)


async def reset_devices(
    session: AsyncSession, client: PasarGuardClient, *, account_id: int
) -> None:
    """Forget every registered device. Idempotent on the panel: a retry resets an empty list."""
    account = await _account(session, account_id)
    try:
        await client.reset_user_hwids(int(account.panel_user_id))
    except PasarGuardError as exc:
        raise ServiceOpError(f"ریست دستگاه‌ها انجام نشد: {exc}") from exc


async def revoke_link(
    session: AsyncSession, client: PasarGuardClient, *, account_id: int
) -> str | None:
    """Rotate the subscription link on the panel and store the new URL.

    Returns the new URL, or None on failure. Shared by the customer's own
    «لینک جدید» and the admin's. Ownership is the caller's job: the customer
    path checks the account is theirs, the admin path is admin-gated.
    """
    account = await get_live_account(session, account_id)
    if account is None:
        return None
    username = str(account.panel_username)
    # No read snapshot across the panel call: once the panel has rotated the
    # link the old one is dead, and a «database is locked» on the write below
    # left the customer holding it. The URL written is the panel's own answer,
    # an absolute value, so nothing here depends on a row read before the call.
    await end_read_transaction(session)
    try:
        live = await client.revoke_user_subscription(username)
    except Exception:
        logger.exception("revoke_sub failed for %s", username)
        # Idempotent recovery: the panel may already have rotated; read the current user.
        try:
            live = await client.get_user(username)
        except Exception:
            logger.exception("get_user after revoke failure also failed for %s", username)
            return None
    sub_url = client.subscription_url_from_response(live)
    if not sub_url:
        return None
    if sub_url != account.subscription_url:
        account.subscription_url = sub_url
        await session.flush()
    return sub_url
