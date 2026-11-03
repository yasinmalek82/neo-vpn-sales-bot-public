"""Provision PasarGuard accounts for approved orders (idempotent).

Phase B1a hybrid:
- Same-plan renew → ``modify_user`` on existing panel username (extend expire + data).
- Different plan → new panel user + new ``PasarGuardAccount`` row (1:N).
- Plan switch (``Order.renew_resets``) → same panel username rebuilt on the new plan.
- Renew from scratch (``Order.renew_mode == "reset"``) → same panel username, volume
  and time set anew; panel groups are never written.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api_client import PasarGuardClient, UserCreate, UserModify, UserResponse
from api_client.exceptions import PasarGuardNotFoundError
from database.base import end_read_transaction
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, ReferralLog
from database.models.order import RENEW_MODE_RESET, TRIAL_KIND_PERSONAL
from services.account_orders import account_was_created_by_an_order
from services.accounts import live_account_clause, trial_label
from services.customer_label import split_label
from services.order_transitions import assert_transition
from services.orders import (
    effective_delivery_data_limit_gb,
    effective_delivery_duration_days,
    effective_delivery_group_ids,
    effective_delivery_hwid_limit,
    find_renewable_account,
    plan_fingerprint,
)
from services.referral import get_referral_reward_for_order, record_referral_reward_for_order
from services.service_names import build_anonymous_panel_username, build_panel_username
from services.store_config import category_title_by_slug, product_title_by_package_slug
from services.wallet import (
    checkout_balance_window,
    has_provision_fail_credit,
    has_valid_retry_debit,
)

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400
_BYTES_PER_GB = 1024**3


class ProvisionError(Exception):
    """Business-rule or panel failure during provisioning."""


def _panel_username_for_order(order: Order) -> str:
    """The panel username for this order, chosen once and never re-rolled.

    **Reusing an already-chosen name is the whole point.** Both generators end
    in a random suffix, so calling this twice for one order used to answer with
    two different names. A `create_user` that reached the panel and lost its
    reply left the order PROVISION_FAILED; the retry then asked for a *new*
    name, found no such user, and created a second panel account — one of them
    orphaned, paid for, and invisible. `_provision_create` stamps the name on
    the order before it calls the panel, so the retry lands on this branch.

    An order with no `service_name` gets an anonymous name rather than the old
    `neo_{order.id}`: that is every reseller order, because their customer's
    name is Persian and stays in the label, and a guessable panel username also
    published the bot's order count to anyone with panel access.
    """
    if order.panel_username:
        return order.panel_username
    if order.service_name:
        try:
            return build_panel_username(order.service_name)
        except ValueError:
            pass
    return build_anonymous_panel_username()


def _buyer_is_reseller(order: Order) -> bool:
    """``order.user`` is eagerly loaded by `provision_order`; never lazy here."""
    return bool(getattr(order.user, "is_reseller", False))


def _expire_unix_from_now(duration_days: int) -> int:
    return int((datetime.now(UTC) + timedelta(days=duration_days)).timestamp())


def _data_limit_bytes(data_limit_gb: int) -> int | None:
    if data_limit_gb <= 0:
        return None
    return data_limit_gb * _BYTES_PER_GB


def expire_to_unix(expire: datetime | int | None) -> int | None:
    """Normalize panel expire value to unix seconds (UTC)."""
    if expire is None:
        return None
    if isinstance(expire, (int, float)):
        return int(expire)
    expire = expire.replace(tzinfo=UTC) if expire.tzinfo is None else expire.astimezone(UTC)
    return int(expire.timestamp())


def compute_renew_expire_unix(
    *,
    current_expire: datetime | int | None,
    duration_days: int,
    now: datetime | None = None,
) -> int:
    """``new_expire = max(current_expire_unix, now_unix) + duration_days * 86400``."""
    now_dt = now or datetime.now(UTC)
    now_unix = int(now_dt.timestamp())
    current_unix = expire_to_unix(current_expire)
    base = max(current_unix, now_unix) if current_unix is not None else now_unix
    return base + duration_days * _SECONDS_PER_DAY


def compute_renew_data_limit(
    *,
    current_data_limit: int | None,
    used_traffic: int,
    new_plan_data_limit_gb: int,
) -> int | None:
    """The absolute cap to write on a renew, or unlimited (None).

    Unlimited if current panel limit is 0/null OR new plan ``data_limit_gb <= 0``.

    ``modify_user`` writes a cap and nothing else: ``used_traffic`` stays, only
    ``reset_user_data_usage`` zeroes it. The customer is promised leftover + new
    volume, so the cap is ``current_limit + new_plan_bytes`` (usage stays counted
    against it). Writing ``remaining + new`` would subtract the usage twice. A
    service already past its cap has no leftover: the cap is lifted from the
    usage, so exactly the new volume is usable.
    """
    if current_data_limit is None or current_data_limit <= 0:
        return None
    if new_plan_data_limit_gb <= 0:
        return None
    base = max(current_data_limit, 0, used_traffic)
    return base + new_plan_data_limit_gb * _BYTES_PER_GB


def resolve_panel_group_ids(
    plan_group_ids: Sequence[int] | None,
    default_group_ids: Sequence[int] | None,
) -> list[int]:
    """Plan wins when non-empty; else defaults. Never merge/union."""
    if plan_group_ids:
        return list(plan_group_ids)
    if default_group_ids:
        return list(default_group_ids)
    return []


async def _maybe_record_referral(session: AsyncSession, *, order_id: int) -> ReferralLog | None:
    from bot.config import get_settings

    reward, _created = await record_referral_reward_for_order(
        session,
        order_id=order_id,
        reward_type="discount",
        reward_value=get_settings().referral_discount_percent,
    )
    return reward


async def _load_account_for_order(session: AsyncSession, order: Order) -> PasarGuardAccount | None:
    if order.panel_username:
        result = await session.execute(
            select(PasarGuardAccount).where(
                PasarGuardAccount.panel_username == order.panel_username
            )
        )
        account = result.scalar_one_or_none()
        if account is not None:
            return account
    result = await session.execute(
        select(PasarGuardAccount)
        .where(PasarGuardAccount.user_id == order.user_id)
        .order_by(PasarGuardAccount.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _release_before_panel_call(session: AsyncSession) -> None:
    """Hold no read snapshot across a panel call.

    The re-read after an intent commit pins a WAL read snapshot; a scheduler
    job commits during the panel call; SQLite then refuses the write that
    follows («database is locked», see ``database.base.end_read_transaction``).
    The panel had already created or extended the service, and the customer
    who paid got «یه مشکلی پیش اومد» and no card until a retry.

    A transaction that has written is left alone: a retry debit flushed by the
    caller must still roll back with a failed retry.
    """
    await end_read_transaction(session)


async def _reread_after_panel_call(session: AsyncSession, order: Order) -> None:
    """The order as the database has it now, before anything is written to it.

    Ending the stale snapshot must not become «write anyway»: that snapshot
    was also what refused to overwrite an order someone else had changed
    during the call (a second admin's approve, a refund). So the row is read
    again in the transaction that writes it, and one that is no longer
    PROVISIONING is left as it is.
    """
    await session.refresh(order)
    if order.status is not OrderStatus.PROVISIONING:
        raise ProvisionError(f"order_changed:{order.status.value}")


# The lifecycle job commits its intent to delete BEFORE it calls the panel. Provisioning that
# meets a fresh intent must not race it: it fails the RETRYABLE way (the order stays paid and is
# retried), never as a refusal that loses the customer's money. An older intent is stale: cleared,
# and that clear is committed before provisioning's own panel call.
DELETE_INTENT_FRESH = timedelta(minutes=10)


async def _settle_delete_intent(session: AsyncSession, account: PasarGuardAccount) -> None:
    if account.delete_requested_at is None:
        return
    stamp = account.delete_requested_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if datetime.now(UTC) - stamp < DELETE_INTENT_FRESH:
        raise ProvisionError("panel_transient")
    account.delete_requested_at = None
    await session.flush()
    await session.commit()


async def _finalize_active(
    session: AsyncSession,
    *,
    order: Order,
    account: PasarGuardAccount,
    panel_user: UserResponse,
    expire_unix: int,
    fingerprint: str,
    client: PasarGuardClient,
    sibling_expiry: str | None = None,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Mark ``order`` ACTIVE and follow the panel's expiry.

    ``sibling_expiry`` is ``"renew"``, ``"switch"`` or ``"reset"`` for an order that
    changed an existing service, ``None`` for a create. One panel service can be backed by
    several ACTIVE orders (the original and every renewal); reminders and the
    expiring filter read ``orders.expires_at`` of each, so the earlier ones must
    follow the panel too or they keep firing for a date that no longer exists.
    Mirrors ``service_ops.extend_days``: absolute value, same transaction. A
    renew only moves an older date later; a switch resets the expiry to
    today + days, which may be earlier, so it overwrites.

    ``expire_unix == 0`` is «بدون انقضا» and is stored as ``NULL``, never as
    1970: a date reads as «expired long ago» to the service list, the stale
    card and the renew match, and a no-expiry service showed up red.
    """
    await _reread_after_panel_call(session, order)
    sub_url = client.subscription_url_from_response(panel_user)
    account.panel_user_id = panel_user.id
    account.panel_username = panel_user.username
    # The bot itself changed this service: the lifecycle job's «limited for N days» clock and
    # any intent to delete belong to the state it was in before.
    account.limited_since = None
    account.delete_requested_at = None
    account.subscription_url = sub_url
    account.plan_fingerprint = fingerprint
    account.note = f"neo order {order.id}"
    if order.customer_label:
        # A reseller's own note about their customer. It is not an identifier,
        # so it never touches `service_name`.
        account.display_name = order.customer_label
    elif order.service_name:
        account.display_name = order.service_name
    elif order.trial_kind == TRIAL_KIND_PERSONAL:
        # Which product it tests: a customer with a direct and a tunnel trial
        # cannot tell two «تست رایگان» rows apart.
        account.display_name = trial_label(
            await category_title_by_slug(session, order.trial_category_slug)
        )
    if order.service_name:
        account.service_name = order.service_name

    order.panel_username = panel_user.username
    now = datetime.now(UTC)
    if order.paid_at is None:
        order.paid_at = now
    order.activated_at = now
    expires_at = datetime.fromtimestamp(expire_unix, tz=UTC) if expire_unix else None
    order.expires_at = expires_at
    assert_transition(OrderStatus.PROVISIONING, OrderStatus.ACTIVE)
    order.status = OrderStatus.ACTIVE
    await session.flush()
    if sibling_expiry is not None:
        stmt = update(Order).where(
            Order.panel_username == panel_user.username,
            Order.status == OrderStatus.ACTIVE,
            Order.id != order.id,
        )
        if sibling_expiry == "renew" and expires_at is not None:
            stmt = stmt.where(Order.expires_at < expires_at)
        await session.execute(stmt.values(expires_at=expires_at))

    reward = await _maybe_record_referral(session, order_id=order.id)
    await _report_sale(session, order)
    from database.models import User
    from services.delivery_outbox import enqueue_delivery

    buyer = await session.get(User, int(order.user_id))
    if buyer is None:
        raise ProvisionError("delivery_owner_missing")
    await enqueue_delivery(
        session,
        event_key=f"service:{order.id}:customer",
        kind="service",
        chat_id=buyer.telegram_id,
        payload={"order_id": order.id, "user_id": buyer.id, "account_id": account.id},
    )
    return account, reward


async def _report_sale(session: AsyncSession, order: Order) -> None:
    """Queue the sale report inside the activation transaction.

    Enqueued, never sent: a Telegram failure here would roll back an
    activation the panel has already performed.
    """
    from database.models import User
    from services.report_events import order_activated
    from services.report_outbox import enqueue_report

    buyer = await session.get(User, int(order.user_id))
    if buyer is None:
        return
    # A giveaway is not a sale. A free trial already reports itself to the
    # customers topic, so reporting it here too put «🛒 فروش انجام شد — ۰ تومان»
    # in the owner's revenue feed — twice the noise and none of the money. A
    # reseller's *paid* trial still counts, because money moved.
    if int(order.price_toman or 0) <= 0:
        return
    # ``order.plan`` is a lazy relationship: touching it here raises
    # MissingGreenlet whenever it was not eagerly loaded, which is most of the
    # provisioning path. Load it explicitly instead.
    plan = await session.get(Plan, int(order.plan_id)) if order.plan_id else None
    window = await checkout_balance_window(session, int(order.id))
    phone = split_label(order.customer_label).phone
    report = order_activated(
        order_id=int(order.id),
        telegram_id=int(buyer.telegram_id),
        username=buyer.username,
        is_reseller=bool(buyer.is_reseller),
        price_toman=int(order.price_toman),
        # The shelf's name, by slug. Never ``plan.name``: that row is internal
        # («volume-10GB-30d-p70000-h0-g6-8») and the owner read it in «فروش».
        product=await product_title_by_package_slug(session, order.store_package_slug),
        is_renewal=order.renew_account_id is not None,
        link_user_id=int(buyer.id),
        service_name=order.service_name,
        buyer_name=buyer.first_name,
        phone=phone,
        panel_username=order.panel_username,
        volume_gb=(
            int(order.delivery_data_limit_gb)
            if order.delivery_data_limit_gb is not None
            else (int(plan.data_limit_gb) if plan else None)
        ),
        # The shared reader: a no-expiry plan row carries 0, which is not «۰ روز».
        duration_days=(
            effective_delivery_duration_days(order, plan) if plan else order.delivery_duration_days
        ),
        balance_before=window[0] if window else None,
        balance_after=window[1] if window else None,
        when=order.activated_at,
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def _queue_trial_report(session: AsyncSession, order: Order) -> None:
    """Trial card, once the panel username is on the order.

    Imported lazily: ``services.trial`` imports ``provision_order`` from this
    module, so a top-level import here would cycle.
    """
    if order.trial_kind is None:
        return
    try:
        from services.trial import enqueue_trial_report

        await enqueue_trial_report(session, order)
    except Exception:
        logger.exception("Could not queue the trial report for order %s", order.id)


async def _report_provision_failed(session: AsyncSession, order: Order, detail: str) -> None:
    """Payment captured, service not delivered — the one report needing a human."""
    from database.models import User
    from services.report_events import provision_failed as build_failure
    from services.report_outbox import enqueue_report

    buyer = await session.get(User, int(order.user_id))
    telegram_id = int(buyer.telegram_id) if buyer else 0
    report = build_failure(
        order_id=int(order.id),
        telegram_id=telegram_id,
        detail=detail,
        username=buyer.username if buyer else None,
        panel_username=order.panel_username,
        link_user_id=int(buyer.id) if buyer else None,
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def _mark_provision_failed(session: AsyncSession, order: Order, exc: BaseException) -> None:
    """Record the failure and queue its report, from one place.

    There were five sites setting PROVISION_FAILED; routing them all through
    here is what stops a sixth being added without a report.
    """
    await _reread_after_panel_call(session, order)
    order.status = OrderStatus.PROVISION_FAILED
    await session.flush()
    await _report_provision_failed(session, order, _failure_detail(exc))


def _failure_detail(exc: BaseException, *, limit: int = 200) -> str:
    """Sanitised reason. Panel errors can quote a URL or a key."""
    raw = str(exc).strip() or exc.__class__.__name__
    return raw[:limit]


def _is_panel_transient(exc: BaseException) -> bool:
    return isinstance(
        exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)
    )


def _reseller_create_outcome_is_unconfirmed(order: Order, exc: BaseException) -> bool:
    """Keep reseller create ambiguity reconcilable instead of refunding it.

    Only 400/422 and authentication 401 prove a create was rejected.  All
    other outcomes may have followed a successful panel write, so this scoped
    reseller path remains PROVISIONING with its durable username.
    """
    if order.trial_kind != "reseller":
        return False
    from services.finance import is_terminal_wallet_provision_rejection

    return not is_terminal_wallet_provision_rejection(exc)


async def _hwid_limit_service_was_created_with(
    session: AsyncSession,
    *,
    account: PasarGuardAccount,
    order: Order,
    plan: Plan,
) -> int:
    """The device limit from the order that first built this panel user.

    A later renew must send that same number. Omitting it lets the panel fall
    back to its global policy, and ``0`` (no device cap) must stay ``0``.
    A plan switch rebuilds the account on the new plan's limit, so the latest
    applied switch wins over the creating order.
    """
    switched = await session.scalar(
        select(Order.delivery_hwid_limit)
        .where(
            Order.panel_username == account.panel_username,
            Order.renew_resets.is_(True),
            Order.status == OrderStatus.ACTIVE,
            Order.delivery_hwid_limit.is_not(None),
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    if switched is not None:
        return int(switched)
    result = await session.execute(
        select(Order.delivery_hwid_limit)
        .where(
            Order.panel_username == account.panel_username,
            Order.delivery_hwid_limit.is_not(None),
        )
        .order_by(Order.id.asc())
        .limit(1)
    )
    raw = result.scalar_one_or_none()
    if raw is not None:
        return int(raw)
    return effective_delivery_hwid_limit(order, plan)


async def _provision_renew(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order: Order,
    plan: Plan,
    account: PasarGuardAccount,
    fingerprint: str,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Extend existing panel user via UserModify (no new neo_{order_id}).

    Durable renew intent (targets + panel_username) is committed BEFORE modify_user.
    Unknown network errors leave the order in PROVISIONING for reconcile — they are
    not treated as definite failure. Resume reconciles panel state before re-extend.
    """
    await _settle_delete_intent(session, account)
    username = account.panel_username
    duration_days = effective_delivery_duration_days(order, plan)
    data_limit_gb = effective_delivery_data_limit_gb(order, plan)
    hwid_limit = await _hwid_limit_service_was_created_with(
        session, account=account, order=order, plan=plan
    )

    async def _finalize_from_panel(
        panel_user: UserResponse, expire_unix: int
    ) -> tuple[PasarGuardAccount, ReferralLog | None]:
        return await _finalize_active(
            session,
            order=order,
            account=account,
            panel_user=panel_user,
            expire_unix=expire_unix,
            fingerprint=fingerprint,
            client=client,
            sibling_expiry="renew",
        )

    # Reconcile path: intent already durable.
    if order.renew_target_expire_unix is not None:
        await _release_before_panel_call(session)
        try:
            panel_user = await client.get_user(username)
        except Exception as exc:
            if _is_panel_transient(exc):
                logger.exception(
                    "Renew reconcile get_user transient for order %s — leaving PROVISIONING",
                    order.id,
                )
                raise ProvisionError("panel_transient") from exc
            await _mark_provision_failed(session, order, exc)
            logger.exception("Renew reconcile get_user failed for order %s", order.id)
            raise

        live_expire = expire_to_unix(panel_user.expire)
        target = int(order.renew_target_expire_unix)
        target_limit = int(order.renew_target_data_limit or 0)
        live_limit = int(panel_user.data_limit or 0)
        # Expiry alone proves nothing for a no-duration volume purchase: it
        # was already equal before the PUT. Usage is deliberately excluded;
        # a customer may have used the service since a successful lost reply.
        if (
            (live_expire or 0) == target
            and live_limit == target_limit
            and panel_user.hwid_limit == hwid_limit
        ):
            if order.panel_username != username:
                order.panel_username = username
                await session.flush()
            return await _finalize_from_panel(panel_user, target)

        # A later external extension/quota change is not proof this purchase
        # landed. Do not lower it to our old target or falsely declare ACTIVE.
        # Keep the durable paid intent for explicit reconciliation, not refund.
        if (live_expire or 0) > target or (
            target_limit != 0 and (live_limit == 0 or live_limit > target_limit)
        ):
            logger.warning("Renew state conflict for order %s; retaining intent", order.id)
            raise ProvisionError("renew_state_conflict")

        # Intent committed but panel not yet extended — apply stored targets once.
        try:
            modify_payload = UserModify(
                expire=target,
                data_limit=target_limit,
                hwid_limit=hwid_limit,
            )
            panel_user = await client.modify_user(username, modify_payload)
        except Exception as exc:
            if _is_panel_transient(exc):
                logger.exception(
                    "Renew modify_user transient for order %s — leaving PROVISIONING",
                    order.id,
                )
                raise ProvisionError("panel_transient") from exc
            await _mark_provision_failed(session, order, exc)
            logger.exception("Renew modify after intent failed for order %s", order.id)
            raise
        expire_unix = expire_to_unix(panel_user.expire) or target
        return await _finalize_from_panel(panel_user, expire_unix)

    # Fresh renew: compute targets, durable-commit intent, then call panel.
    await _release_before_panel_call(session)
    try:
        live = await client.get_user(username)
    except Exception as exc:
        if _is_panel_transient(exc):
            logger.exception(
                "Renew get_user transient for order %s — leaving PROVISIONING", order.id
            )
            raise ProvisionError("panel_transient") from exc
        await _mark_provision_failed(session, order, exc)
        logger.exception("Renew get_user failed for order %s", order.id)
        raise

    if duration_days is None:
        # «بدون انقضا» renew keeps whatever expire the panel already has (often None).
        new_expire = expire_to_unix(live.expire) or 0
    else:
        new_expire = compute_renew_expire_unix(
            current_expire=live.expire,
            duration_days=int(duration_days),
        )
    new_limit = compute_renew_data_limit(
        current_data_limit=live.data_limit,
        used_traffic=live.used_traffic,
        new_plan_data_limit_gb=data_limit_gb,
    )
    # Commit intent + targets BEFORE panel call (anti double-extend).
    await _reread_after_panel_call(session, order)
    order.panel_username = username
    order.renew_target_expire_unix = new_expire
    order.renew_target_data_limit = 0 if new_limit is None else int(new_limit)
    await session.flush()
    await session.commit()
    await session.refresh(order)
    await session.refresh(account)
    await _release_before_panel_call(session)

    try:
        modify_payload = UserModify(
            expire=new_expire,
            data_limit=0 if new_limit is None else new_limit,
            hwid_limit=hwid_limit,
        )
        panel_user = await client.modify_user(username, modify_payload)
    except Exception as exc:
        if _is_panel_transient(exc):
            logger.exception(
                "Renew modify_user transient for order %s — leaving PROVISIONING",
                order.id,
            )
            raise ProvisionError("panel_transient") from exc
        await _mark_provision_failed(session, order, exc)
        logger.exception("Renew provisioning failed for order %s", order.id)
        raise

    expire_unix = expire_to_unix(panel_user.expire) or new_expire
    return await _finalize_from_panel(panel_user, expire_unix)


# A panel may round an expiry it stores; a switch that landed within this many
# seconds of its target is the switch this order already applied.
_SWITCH_EXPIRE_TOLERANCE_SECONDS = 120


def _switch_applied(
    panel_user: UserResponse,
    *,
    expire_unix: int,
    data_limit: int,
    hwid_limit: int,
    group_ids: Sequence[int] | None,
) -> bool:
    """Whether the panel already holds every absolute value this switch writes.

    ``group_ids=None`` is a switch that does not write groups (an account with
    no creating order): they are not compared either.

    Not «expire >= target» as for an extension: a switch to a shorter plan
    lowers the expiry, and that test would call an unapplied switch done.
    Expiry and volume alone are not enough either: a same-size plan of another
    panel group, on a service bought minutes ago, matches both before anything
    was written, and the account would stay on the group the customer left.
    """
    live_expire = expire_to_unix(panel_user.expire) or 0
    live_limit = int(panel_user.data_limit or 0)
    return (
        abs(live_expire - expire_unix) <= _SWITCH_EXPIRE_TOLERANCE_SECONDS
        and live_limit == data_limit
        and int(panel_user.hwid_limit or 0) == hwid_limit
        and (group_ids is None or sorted(panel_user.group_ids or []) == sorted(group_ids))
    )


async def _provision_switch(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order: Order,
    plan: Plan,
    account: PasarGuardAccount,
    fingerprint: str,
    default_group_ids: Sequence[int] | None,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Rebuild an existing account on a new plan: same username, same link.

    Expiry from today, a fresh volume (usage reset), and the new plan's panel
    groups and device limit — the leftover of the old plan is dropped, as the
    confirm screen told the customer. Targets are committed before any panel
    call, then usage is reset, then the absolute values are written: a retry
    that finds the targets on the panel knows the reset came first, and one
    that does not may reset again, which costs the customer nothing.
    """
    await _settle_delete_intent(session, account)
    username = account.panel_username
    duration_days = effective_delivery_duration_days(order, plan)
    data_limit_gb = effective_delivery_data_limit_gb(order, plan)
    hwid_limit = effective_delivery_hwid_limit(order, plan)
    # An account no order of ours created (imported from the old bot) keeps the
    # panel groups the owner gave it: they were never a plan's to replace.
    write_groups = await account_was_created_by_an_order(session, account)
    group_ids = resolve_panel_group_ids(
        effective_delivery_group_ids(order, plan), default_group_ids
    )
    if write_groups and not group_ids:
        # A user written with no groups loses every inbound; refuse before
        # touching the panel or the order.
        raise ProvisionError("no_panel_group")

    if order.renew_target_expire_unix is None:
        order.panel_username = username
        order.renew_target_expire_unix = (
            0 if duration_days is None else _expire_unix_from_now(int(duration_days))
        )
        order.renew_target_data_limit = _data_limit_bytes(data_limit_gb) or 0
        await session.flush()
        await session.commit()
        await session.refresh(order)
        await session.refresh(account)
    target_expire = int(order.renew_target_expire_unix or 0)
    target_limit = int(order.renew_target_data_limit or 0)

    async def _panel(step: str, call: Any) -> UserResponse:
        try:
            return cast(UserResponse, await call)
        except Exception as exc:
            if _is_panel_transient(exc):
                logger.exception(
                    "Switch %s transient for order %s — leaving PROVISIONING", step, order.id
                )
                raise ProvisionError("panel_transient") from exc
            await _mark_provision_failed(session, order, exc)
            logger.exception("Switch %s failed for order %s", step, order.id)
            raise

    await _release_before_panel_call(session)
    live = await _panel("get_user", client.get_user(username))
    if not _switch_applied(
        live,
        expire_unix=target_expire,
        data_limit=target_limit,
        hwid_limit=hwid_limit,
        group_ids=group_ids if write_groups else None,
    ):
        await _panel("reset", client.reset_user_data_usage(username))
        live = await _panel(
            "modify_user",
            client.modify_user(
                username,
                UserModify(
                    expire=target_expire,
                    data_limit=target_limit,
                    hwid_limit=hwid_limit,
                    # An empty list would strip every inbound from the account,
                    # and an imported account keeps the groups it has.
                    group_ids=(group_ids or None) if write_groups else None,
                ),
            ),
        )
    return await _finalize_active(
        session,
        order=order,
        account=account,
        panel_user=live,
        expire_unix=expire_to_unix(live.expire) or target_expire,
        fingerprint=fingerprint,
        client=client,
        sibling_expiry="switch",
    )


async def _device_limit_to_keep(
    session: AsyncSession, *, account: PasarGuardAccount, order: Order, live: UserResponse
) -> int | None:
    """The device limit a renew from scratch leaves the service with.

    The same source a renewal uses: the latest applied switch, else the order
    that first built this panel user (this order excluded, it is the one being
    applied). An account with no such order (imported) keeps what the panel
    holds, and ``None`` there means the panel's global policy: nothing is sent.
    """
    switched = await session.scalar(
        select(Order.delivery_hwid_limit)
        .where(
            Order.panel_username == account.panel_username,
            Order.id != order.id,
            Order.renew_resets.is_(True),
            Order.status == OrderStatus.ACTIVE,
            Order.delivery_hwid_limit.is_not(None),
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    if switched is not None:
        return int(switched)
    first = await session.scalar(
        select(Order.delivery_hwid_limit)
        .where(
            Order.panel_username == account.panel_username,
            Order.id != order.id,
            Order.delivery_hwid_limit.is_not(None),
        )
        .order_by(Order.id.asc())
        .limit(1)
    )
    if first is not None:
        return int(first)
    return None if live.hwid_limit is None else int(live.hwid_limit)


def _scratch_applied(
    panel_user: UserResponse, *, expire_unix: int, data_limit: int, hwid_limit: int | None
) -> bool:
    """Whether the panel already holds every absolute value a renew from scratch writes.

    Equality, like a switch: an extension's «expire >= target» would call a
    shorter new period applied. **Usage is not compared**: after the first
    application the customer is using the service again, and a retry that read
    «used > 0» as «not applied» would wipe it a second time.
    """
    live_expire = expire_to_unix(panel_user.expire) or 0
    return (
        abs(live_expire - expire_unix) <= _SWITCH_EXPIRE_TOLERANCE_SECONDS
        and int(panel_user.data_limit or 0) == data_limit
        and (hwid_limit is None or int(panel_user.hwid_limit or 0) == hwid_limit)
    )


async def _provision_from_scratch(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order: Order,
    plan: Plan,
    account: PasarGuardAccount,
    fingerprint: str,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """«Renew from scratch»: the volume and time set anew on the same account.

    The third operation next to extend (``_provision_renew`` adds) and switch
    (``_provision_switch`` resets AND replaces panel groups and device limit).
    It writes absolute values: expiry = today + days, usage reset, the bought
    volume; it **never sends ``group_ids``** and keeps the service's device
    limit. The leftover is dropped, as the summary said.

    Targets are committed first, then the live user is read, and a retry is
    recognised by equality over every value written (``_scratch_applied``).
    The usage reset comes before the write: a retry that finds the values on
    the panel knows both happened; one that does not may reset again, which
    costs the customer nothing, and converges on the same end state.
    """
    username = account.panel_username
    duration_days = effective_delivery_duration_days(order, plan)
    data_limit_gb = effective_delivery_data_limit_gb(order, plan)

    if order.renew_target_expire_unix is None:
        order.panel_username = username
        order.renew_target_expire_unix = (
            0 if duration_days is None else _expire_unix_from_now(int(duration_days))
        )
        order.renew_target_data_limit = _data_limit_bytes(data_limit_gb) or 0
        await session.flush()
        await session.commit()
        await session.refresh(order)
        await session.refresh(account)
    target_expire = int(order.renew_target_expire_unix or 0)
    target_limit = int(order.renew_target_data_limit or 0)

    async def _panel(step: str, call: Any) -> UserResponse:
        try:
            return cast(UserResponse, await call)
        except Exception as exc:
            if _is_panel_transient(exc):
                logger.exception(
                    "From-scratch %s transient for order %s — leaving PROVISIONING",
                    step,
                    order.id,
                )
                raise ProvisionError("panel_transient") from exc
            await _mark_provision_failed(session, order, exc)
            logger.exception("From-scratch %s failed for order %s", step, order.id)
            raise

    await _release_before_panel_call(session)
    live = await _panel("get_user", client.get_user(username))
    hwid_limit = await _device_limit_to_keep(session, account=account, order=order, live=live)
    await _release_before_panel_call(session)
    if not _scratch_applied(
        live, expire_unix=target_expire, data_limit=target_limit, hwid_limit=hwid_limit
    ):
        await _panel("reset", client.reset_user_data_usage(username))
        live = await _panel(
            "modify_user",
            client.modify_user(
                username,
                UserModify(
                    expire=target_expire,
                    data_limit=target_limit,
                    hwid_limit=hwid_limit,
                    # Never group_ids: this operation does not touch panel groups.
                ),
            ),
        )
    if hwid_limit is not None:
        # The order's snapshot is what a later renew reads as the device limit
        # the service was made with; it must say what the service really has,
        # not the group's custom default.
        await _reread_after_panel_call(session, order)
        order.delivery_hwid_limit = hwid_limit
        await session.flush()
    return await _finalize_active(
        session,
        order=order,
        account=account,
        panel_user=live,
        expire_unix=expire_to_unix(live.expire) or target_expire,
        fingerprint=fingerprint,
        client=client,
        sibling_expiry="reset",
    )


async def _provision_create(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order: Order,
    plan: Plan,
    fingerprint: str,
    default_group_ids: Sequence[int] | None,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Create a new panel user + new PasarGuardAccount row (1:N)."""
    group_ids = resolve_panel_group_ids(
        effective_delivery_group_ids(order, plan), default_group_ids
    )
    if not group_ids:
        # An empty group_ids strips every inbound. Refuse before the username
        # stamp, the report and the panel call.
        raise ProvisionError("no_panel_group")
    username = _panel_username_for_order(order)
    fresh_name = order.panel_username != username
    for _attempt in range(5):
        taken = (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.panel_username == username)
            )
        ).scalar_one_or_none()
        if taken is None:
            break
        if taken.user_id == order.user_id:
            await _settle_delete_intent(session, taken)
            break
        if not fresh_name:
            # A name already stamped on this order may have reached the panel: never re-roll it.
            raise ProvisionError("username_collision")
        # Another customer's (deleted) service holds this name: it is taken. Roll a new one
        # through the same generator; never revive someone else's row.
        order.panel_username = None
        username = _panel_username_for_order(order)
    else:
        raise ProvisionError("username_collision")
    if order.panel_username != username:
        # Commit the stamped username BEFORE the panel call — same pattern as
        # `_provision_renew`. A flush alone dies with the caller's rollback, so
        # the retry would mint a new name and buy a second panel account.
        # See `_panel_username_for_order`.
        order.panel_username = username
        # The trial card names this config. It has to be queued in the same
        # transaction as the stamp, or a crash after the commit would keep the
        # username and lose the report.
        await _queue_trial_report(session, order)
        await session.flush()
        await session.commit()
        await session.refresh(order)
    else:
        await _queue_trial_report(session, order)
    duration_days = effective_delivery_duration_days(order, plan)
    data_limit_gb = effective_delivery_data_limit_gb(order, plan)
    hwid_limit = (
        int(order.delivery_hwid_limit)
        if order.delivery_hwid_limit is not None
        else int(plan.hwid_limit)
    )

    payload = UserCreate(
        username=username,
        data_limit=_data_limit_bytes(data_limit_gb),
        expire=(None if duration_days is None else _expire_unix_from_now(int(duration_days))),
        group_ids=group_ids,
        hwid_limit=hwid_limit,
        note=f"neo order {order.id}",
    )

    await _release_before_panel_call(session)
    try:
        try:
            panel_user = await client.get_user(username)
        except PasarGuardNotFoundError:
            panel_user = await client.create_user(payload)
    except Exception as exc:
        if _is_panel_transient(exc) or _reseller_create_outcome_is_unconfirmed(order, exc):
            logger.exception(
                "Create provisioning transient for order %s — leaving PROVISIONING", order.id
            )
            raise
        await _mark_provision_failed(session, order, exc)
        logger.exception("Create provisioning failed for order %s", order.id)
        raise

    # Prefer existing mapping for this panel username (idempotent create resume).
    acct_result = await session.execute(
        select(PasarGuardAccount).where(PasarGuardAccount.panel_username == panel_user.username)
    )
    account = acct_result.scalar_one_or_none()
    if account is not None and account.user_id != order.user_id:
        # Never adopt (or revive) another customer's row.
        raise ProvisionError("username_collision")
    if account is not None and account.deleted_at is not None:
        # The panel user was re-created under a name the lifecycle job deleted, by the SAME
        # owner: the unique index keeps one row per name, so the row is brought back.
        account.deleted_at = None
        account.limited_since = None
        account.delete_requested_at = None
        account.panel_user_id = panel_user.id
    if account is None:
        account = PasarGuardAccount(
            user_id=order.user_id,
            panel_user_id=panel_user.id,
            panel_username=panel_user.username,
            subscription_url=None,
            note=f"neo order {order.id}",
            plan_fingerprint=fingerprint,
            display_name=order.customer_label or order.service_name,
            service_name=order.service_name,
        )
        session.add(account)
        await session.flush()

    expire_unix = expire_to_unix(panel_user.expire)
    if expire_unix is None:
        expire_unix = 0 if duration_days is None else _expire_unix_from_now(int(duration_days))
    return await _finalize_active(
        session,
        order=order,
        account=account,
        panel_user=panel_user,
        expire_unix=expire_unix,
        fingerprint=fingerprint,
        client=client,
    )


async def _provision_owned_order(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order_id: int,
    default_group_ids: Sequence[int] | None = None,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Create or renew panel user and mark order ACTIVE.

    Valid entry: PAYMENT_APPROVED or retry from PROVISION_FAILED.
    PROVISIONING is allowed to resume after a crash.
    Already ACTIVE → return existing PasarGuardAccount (+ existing reward if any).

    Same-plan: extend via ``modify_user``. Different plan: new panel user + row.
    """
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.plan), selectinload(Order.user))
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise ProvisionError("order_not_found")

    if order.status == OrderStatus.ACTIVE:
        account = await _load_account_for_order(session, order)
        if account is not None:
            reward = await get_referral_reward_for_order(session, order.id)
            return account, reward
        raise ProvisionError("active_without_account")

    if order.status not in {
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISION_FAILED,
        OrderStatus.PROVISIONING,
    }:
        raise ProvisionError(f"invalid_status:{order.status.value}")

    if await has_provision_fail_credit(session, order.id) and not await has_valid_retry_debit(
        session,
        user_id=order.user_id,
        order_id=order.id,
        amount_toman=int(order.price_toman),
    ):
        raise ProvisionError("retry_payment_required")

    if order.status != OrderStatus.PROVISIONING:
        assert_transition(order.status, OrderStatus.PROVISIONING)
        order.status = OrderStatus.PROVISIONING
        await session.flush()

    plan = order.plan
    # Fingerprint from delivery snapshot when present (legacy → plan).
    if order.delivery_data_limit_gb is not None and order.delivery_duration_days is not None:
        if int(order.delivery_data_limit_gb) > 0:
            fingerprint = (
                f"volume:{int(order.delivery_data_limit_gb)}:{int(order.delivery_duration_days)}"
            )
        else:
            hwid = (
                int(order.delivery_hwid_limit)
                if order.delivery_hwid_limit is not None
                else int(plan.hwid_limit)
            )
            fingerprint = f"unlimited:{hwid}:{int(order.delivery_duration_days)}"
    else:
        fingerprint = plan_fingerprint(plan)

    # Durable force_new; migrate legacy sentinel without clearing choice on retry.
    if order.panel_password == "__force_new__":
        order.force_new = True
        order.panel_password = None
        await session.flush()
    force_new = bool(order.force_new)
    renewable = None
    if not force_new and order.renew_account_id is not None:
        acct_res = await session.execute(
            select(PasarGuardAccount).where(
                PasarGuardAccount.id == order.renew_account_id,
                PasarGuardAccount.user_id == order.user_id,
                live_account_clause(),
            )
        )
        renewable = acct_res.scalar_one_or_none()
    # Guessing by fingerprint is a customer-only convenience. A reseller owns
    # one service per customer, so «۵۰ گیگ / ۳۰ روز» matches the last walk-in
    # as readily as the one standing there now — and a match here **extends**
    # that other person's service instead of creating one. The shop pays, their
    # new customer gets nothing, and an unrelated customer quietly gets a
    # second month. An explicit `renew_account_id` still works: that is the
    # shop choosing a named service from «مشتری‌های من», not the bot guessing.
    # A switch named its account. If that account is gone, guessing another by
    # plan shape could stack onto an unrelated service; a new one is safer.
    if (
        renewable is None
        and not force_new
        and not order.renew_resets
        and order.renew_mode != RENEW_MODE_RESET
        and not _buyer_is_reseller(order)
    ):
        renewable = await find_renewable_account(session, order.user_id, fingerprint)

    # Dispatch on the explicit marker only: never inferred from a missing plan
    # slug or a plan shape. A renew from scratch that lost its account creates
    # a new service, like a switch, rather than extending someone else's.
    if renewable is not None and order.renew_mode == RENEW_MODE_RESET:
        return await _provision_from_scratch(
            session,
            client,
            order=order,
            plan=plan,
            account=renewable,
            fingerprint=fingerprint,
        )
    if renewable is not None and order.renew_resets:
        return await _provision_switch(
            session,
            client,
            order=order,
            plan=plan,
            account=renewable,
            fingerprint=fingerprint,
            default_group_ids=default_group_ids,
        )
    if renewable is not None:
        return await _provision_renew(
            session,
            client,
            order=order,
            plan=plan,
            account=renewable,
            fingerprint=fingerprint,
        )

    return await _provision_create(
        session,
        client,
        order=order,
        plan=plan,
        fingerprint=fingerprint,
        default_group_ids=default_group_ids,
    )


async def provision_order(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    order_id: int,
    default_group_ids: Sequence[int] | None = None,
) -> tuple[PasarGuardAccount, ReferralLog | None]:
    """Serialize one order through final caller commit, without holding a DB snapshot."""
    from database.provision_lock import ProvisionBusy, provision_ownership

    try:
        async with provision_ownership(session, order_id):
            return await _provision_owned_order(
                session, client, order_id=order_id, default_group_ids=default_group_ids
            )
    except ProvisionBusy as exc:
        raise ProvisionError("provision_busy") from exc
