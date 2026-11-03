"""User account overview from local mapping + live PasarGuard panel."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api_client import PasarGuardClient, UserResponse
from api_client.exceptions import PasarGuardError
from database.base import end_read_transaction
from database.models import Order, OrderStatus, PasarGuardAccount
from database.models.order import TRIAL_KIND_PERSONAL

logger = logging.getLogger(__name__)

RECENT_ORDERS_LIMIT = 10
_BYTES_PER_GB = 1024**3
_GB_ROUND_TOLERANCE = 0.005


@dataclass(frozen=True)
class OrderSummary:
    order_id: int
    status: str
    price_toman: int
    created_at: datetime
    expires_at: datetime | None = None


@dataclass(frozen=True)
class AccountOverview:
    """Snapshot for account UX. Bytes and timestamps are raw; handlers format."""

    has_account: bool
    has_orders: bool
    stale: bool
    account_id: int | None = None
    panel_username: str | None = None
    display_name: str | None = None
    status: str | None = None
    expire: datetime | None = None
    used_traffic: int | None = None
    data_limit: int | None = None
    duration_days: int | None = None
    subscription_url: str | None = None
    recent_orders: list[OrderSummary] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.has_account and not self.has_orders

    @property
    def is_live(self) -> bool:
        return self.has_account and not self.stale


def normalize_expire(expire: datetime | int | None) -> datetime | None:
    """Normalize panel expire (unix int or datetime) to timezone-aware UTC."""
    if expire is None:
        return None
    if isinstance(expire, (int, float)):
        return datetime.fromtimestamp(int(expire), tz=UTC)
    if expire.tzinfo is None:
        return expire.replace(tzinfo=UTC)
    return expire.astimezone(UTC)


def format_traffic_gb(nbytes: int) -> str:
    """Human GB label (no unit suffix) from a byte count."""
    gb = nbytes / _BYTES_PER_GB
    if abs(gb - round(gb)) < _GB_ROUND_TOLERANCE:
        return str(int(round(gb)))
    return f"{gb:.2f}"


def data_limit_bytes(data_limit_gb: int | None) -> int | None:
    """Plan GB → bytes; None / <= 0 means unlimited (matches provisioning)."""
    if data_limit_gb is None or data_limit_gb <= 0:
        return None
    return data_limit_gb * _BYTES_PER_GB


def live_account_clause() -> ColumnElement[bool]:
    """``WHERE`` for a service that has not been deleted by the lifecycle job.

    **Every loader of services goes through this** (customer list, reseller list,
    admin card, renew matching, reminders, the lifecycle job itself): a deleted
    account keeps its row (orders, payments and the ledger point at it) but is
    not a service any more. A loader that forgets it shows a service whose panel
    user is gone. Tests: ``tests/unit/test_soft_deleted_accounts.py``.
    """
    return PasarGuardAccount.deleted_at.is_(None)


async def get_live_account(session: AsyncSession, account_id: int) -> PasarGuardAccount | None:
    """``session.get`` for an account id from a button, minus a deleted account."""
    account = await session.get(PasarGuardAccount, int(account_id))
    if account is None or account.deleted_at is not None:
        return None
    return account


async def _load_accounts(session: AsyncSession, user_id: int) -> list[PasarGuardAccount]:
    result = await session.execute(
        select(PasarGuardAccount)
        .where(PasarGuardAccount.user_id == user_id, live_account_clause())
        .order_by(PasarGuardAccount.id.desc())
    )
    return list(result.scalars().all())


def account_is_named(account: PasarGuardAccount) -> bool:
    """True when the account has a customer-facing service_name or display_name."""
    return bool((account.service_name or "").strip() or (account.display_name or "").strip())


# A personal trial never asks for a name. The panel username is anonymous, so
# the list would otherwise show something like ``neo_a7f3`` or hide the row.
PERSONAL_TRIAL_LABEL = "تست رایگان"

# Emoji and pictographs an owner puts in a group title («حجمی ویژه ⭐️»). Customer
# copy allows one emoji per line (BRAND.md §3), so a title is shown without them.
_PICTOGRAPHS = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]")


def plain_title(title: str | None) -> str:
    """A store group's title without its emoji: «حجمی ویژه ⭐️» → «حجمی ویژه»."""
    return re.sub(r"\s+", " ", _PICTOGRAPHS.sub("", title or "")).strip()


def trial_label(group_title: str | None) -> str:
    """«تست حجمی ویژه»: which product the trial tests, for the customer and the owner."""
    plain = plain_title(group_title)
    return f"تست {plain}" if plain else PERSONAL_TRIAL_LABEL


def account_label(account: PasarGuardAccount) -> str:
    """Visible list label.

    A chosen service name keeps the panel username (``yasin_u7jf``). A trial
    has only a display label, and that label is what the customer recognizes.
    """
    username = (account.panel_username or "").strip()
    display = (account.display_name or "").strip()
    service = (account.service_name or "").strip()
    if display and not service:
        return display
    if username:
        return username
    return display or service or "—"


def service_list_label(account: PasarGuardAccount, *, reseller: bool) -> str:
    """The name «سرویس‌های من» shows for this service, for the person looking.

    A customer's list labels a row by `account_label`; a shop's list leads with
    the note they typed about their customer («رضا | 0912…»). Anything that
    names a service back to its owner (the renew summary) uses this, so the
    two screens never call one service by two names.
    """
    if not reseller:
        return account_label(account)
    named = (account.display_name or account.service_name or "").strip()
    return named or account.panel_username or "—"


async def _personal_trial_usernames(session: AsyncSession, user_id: int) -> set[str]:
    result = await session.execute(
        select(Order.panel_username).where(
            Order.user_id == user_id,
            Order.trial_kind == TRIAL_KIND_PERSONAL,
            Order.panel_username.is_not(None),
        )
    )
    return {name for name in result.scalars() if name}


async def list_named_services(session: AsyncSession, user_id: int) -> list[PasarGuardAccount]:
    """Accounts that count in «سرویس‌های من».

    Named services, plus a personal trial. A trial has no chosen name, and
    dropping it left the screen the repeat-tap message points at empty.
    Legacy unnamed rows that are not a trial stay hidden.
    """
    accounts = await _load_accounts(session, user_id)
    trial_usernames = await _personal_trial_usernames(session, user_id)
    visible: list[PasarGuardAccount] = []
    for account in accounts:
        is_trial = (account.panel_username or "") in trial_usernames
        if is_trial and not account_is_named(account):
            account.display_name = PERSONAL_TRIAL_LABEL
        if account_is_named(account):
            visible.append(account)
    return visible


# One page of a reseller's services. Ten is not arbitrary: the list screen asks
# the panel for live status once per row, so a page is also a batch of HTTP
# calls. A shop with thirty customers on one screen would be thirty sequential
# requests every time they opened it.
RESELLER_PAGE_SIZE = 10


@dataclass(frozen=True)
class ServicePage:
    rows: list[PasarGuardAccount]
    offset: int
    total: int
    page_size: int
    query: str | None

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.rows) < self.total

    @property
    def prev_offset(self) -> int:
        return max(0, self.offset - self.page_size)

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.rows)


async def list_services_page(
    session: AsyncSession,
    user_id: int,
    *,
    offset: int = 0,
    limit: int = RESELLER_PAGE_SIZE,
    query: str | None = None,
) -> ServicePage:
    """One page of someone's services, newest first, optionally searched.

    ``query`` matches the label the reseller gave the service — their own
    customer's name or phone number. That is the only handle a shop has on a
    service; nobody remembers which `neo_a7f3` belongs to which walk-in.

    Sorted newest-first rather than by expiry on purpose: expiry lives on the
    panel, and sorting by it would mean fetching every service on every page
    load, which is exactly the cost this pagination exists to avoid.
    """
    offset = max(0, int(offset))
    named = (PasarGuardAccount.display_name.is_not(None)) | (
        PasarGuardAccount.service_name.is_not(None)
    )
    clauses = [PasarGuardAccount.user_id == user_id, named, live_account_clause()]

    cleaned = (query or "").strip()
    if cleaned:
        # Usually one pattern; two when the query holds Persian or Arabic
        # digits. A phone is normalized to ASCII when it is written, so «۶۷۸۹»
        # has to be tried as `6789` — while a *name* like «رضا ۲» was stored
        # with the digits the shop typed, so the raw form has to be tried too.
        from services.customer_label import search_patterns

        matches = None
        for pattern in search_patterns(cleaned):
            clause = (
                func.lower(PasarGuardAccount.display_name).like(pattern)
                | func.lower(PasarGuardAccount.service_name).like(pattern)
                | func.lower(PasarGuardAccount.panel_username).like(pattern)
            )
            matches = clause if matches is None else (matches | clause)
        if matches is not None:
            clauses.append(matches)

    total = int(
        await session.scalar(select(func.count()).select_from(PasarGuardAccount).where(*clauses))
        or 0
    )
    rows = list(
        (
            await session.execute(
                select(PasarGuardAccount)
                .where(*clauses)
                .order_by(PasarGuardAccount.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # `account_is_named` is the UX rule; the SQL above is its index-friendly
    # twin. Filtering again keeps the two from drifting apart silently.
    rows = [row for row in rows if account_is_named(row)]
    return ServicePage(
        rows=rows, offset=offset, total=total, page_size=limit, query=cleaned or None
    )


async def find_account_id_for_order(session: AsyncSession, order: Order) -> int | None:
    """Resolve PasarGuardAccount id for an ACTIVE order (renew id or panel username)."""
    if order.renew_account_id is not None:
        renew_id = await session.scalar(
            select(PasarGuardAccount.id).where(
                PasarGuardAccount.id == int(order.renew_account_id), live_account_clause()
            )
        )
        return int(renew_id) if renew_id is not None else None
    username = (order.panel_username or "").strip()
    if not username:
        return None
    result = await session.execute(
        select(PasarGuardAccount.id).where(
            PasarGuardAccount.panel_username == username, live_account_clause()
        )
    )
    found = result.scalar_one_or_none()
    return int(found) if found is not None else None


def _pick_account(
    accounts: list[PasarGuardAccount], preferred: Order | None
) -> PasarGuardAccount | None:
    """Prefer account matching preferred order panel_username; else latest."""
    if not accounts:
        return None
    if preferred is not None and preferred.panel_username:
        for account in accounts:
            if account.panel_username == preferred.panel_username:
                return account
    return accounts[0]


async def _load_recent_orders(session: AsyncSession, user_id: int, limit: int) -> list[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .options(selectinload(Order.plan))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _preferred_order(orders: list[Order]) -> Order | None:
    """Latest ACTIVE order (list is newest-first), else most recent."""
    for order in orders:
        if order.status == OrderStatus.ACTIVE:
            return order
    return orders[0] if orders else None


def _overview_from_db(
    *,
    account: PasarGuardAccount | None,
    preferred: Order | None,
    recent_orders: list[OrderSummary],
    stale: bool,
) -> AccountOverview:
    status: str | None = None
    expire: datetime | None = None
    data_limit: int | None = None
    duration_days: int | None = None
    if preferred is not None:
        status = preferred.status.value
        expire = normalize_expire(preferred.expires_at)
        plan = preferred.plan
        if plan is not None:
            data_limit = data_limit_bytes(plan.data_limit_gb)
            duration_days = int(plan.duration_days) if plan.duration_days else None
    return AccountOverview(
        has_account=account is not None,
        has_orders=bool(recent_orders),
        stale=stale,
        account_id=account.id if account else None,
        panel_username=account.panel_username if account else None,
        display_name=(account.display_name or account.service_name) if account else None,
        status=status,
        expire=expire,
        used_traffic=None,
        data_limit=data_limit,
        duration_days=duration_days,
        subscription_url=account.subscription_url if account else None,
        recent_orders=recent_orders,
    )


def _order_summaries(orders: list[Order]) -> list[OrderSummary]:
    return [
        OrderSummary(
            order_id=order.id,
            status=order.status.value,
            price_toman=order.price_toman,
            created_at=order.created_at,
            expires_at=order.expires_at,
        )
        for order in orders
    ]


async def _refresh_from_panel(
    client: PasarGuardClient, account: PasarGuardAccount
) -> tuple[UserResponse | None, bool]:
    """Return (live user, stale). Never invent endpoints — get_user only."""
    try:
        live = await client.get_user(account.panel_username)
    except PasarGuardError:
        logger.warning("Panel get_user failed for %s; using DB fallback", account.panel_username)
        return None, True
    except Exception:
        logger.exception("Unexpected panel error for %s; using DB fallback", account.panel_username)
        return None, True
    return live, False


async def get_account_overview(
    session: AsyncSession,
    *,
    user_id: int,
    client: PasarGuardClient,
    orders_limit: int = RECENT_ORDERS_LIMIT,
    account_id: int | None = None,
) -> AccountOverview:
    """Summarize PasarGuardAccount + recent orders, with live panel refresh.

    When ``account_id`` is set, load that account for the user (detail screen).
    On panel error, fall back to DB fields and set ``stale=True``.
    UserResponse.used_traffic / data_limit / expire are the usage source
    (get_user_usage is a time-series endpoint and is not required here).
    """
    accounts = await _load_accounts(session, user_id)
    orders = await _load_recent_orders(session, user_id, orders_limit)

    if account_id is not None:
        account = next((item for item in accounts if item.id == account_id), None)
        if account is None:
            return AccountOverview(
                has_account=False,
                has_orders=bool(orders),
                stale=False,
                recent_orders=_order_summaries(orders),
            )
        scoped = [order for order in orders if order.panel_username == account.panel_username]
        preferred = _preferred_order(scoped) if scoped else None
        summaries = _order_summaries(scoped if scoped else orders)
    else:
        summaries = _order_summaries(orders)
        if not accounts and not orders:
            return AccountOverview(has_account=False, has_orders=False, stale=False)
        preferred = _preferred_order(orders)
        account = _pick_account(accounts, preferred)

    if account is None:
        return _overview_from_db(
            account=None,
            preferred=preferred,
            recent_orders=summaries,
            stale=False,
        )

    # The panel call below is a network await: let go of the read snapshot so a
    # job committing meanwhile cannot lock the subscription-URL write after it.
    await end_read_transaction(session)
    live, stale = await _refresh_from_panel(client, account)
    if live is None:
        return _overview_from_db(
            account=account,
            preferred=preferred,
            recent_orders=summaries,
            stale=stale,
        )

    # Keep DB subscription_url stable on refresh — only revoke_sub may rotate the link.
    # Fill from panel only when the stored URL is empty/None.
    panel_sub = client.subscription_url_from_response(live)
    sub_url: str | None
    if account.subscription_url:
        sub_url = account.subscription_url
    else:
        sub_url = panel_sub
        if panel_sub:
            account.subscription_url = panel_sub
            await session.flush()

    duration_days: int | None = None
    if preferred is not None and preferred.plan is not None and preferred.plan.duration_days:
        duration_days = int(preferred.plan.duration_days)

    return AccountOverview(
        has_account=True,
        has_orders=bool(summaries),
        stale=False,
        account_id=account.id,
        panel_username=live.username,
        display_name=account.display_name or account.service_name,
        status=live.status.value if live.status is not None else None,
        expire=normalize_expire(live.expire),
        used_traffic=live.used_traffic,
        data_limit=live.data_limit,
        duration_days=duration_days,
        subscription_url=sub_url,
        recent_orders=summaries,
    )


async def service_expire_hint(session: AsyncSession, account: PasarGuardAccount) -> datetime | None:
    """Best-effort expire from latest ACTIVE order linked to this panel username."""
    result = await session.execute(
        select(Order)
        .where(
            Order.user_id == account.user_id,
            Order.panel_username == account.panel_username,
            Order.status == OrderStatus.ACTIVE,
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    order = result.scalar_one_or_none()
    if order is None:
        return None
    return normalize_expire(order.expires_at)


@dataclass(frozen=True)
class LiveStatus:
    """What one list row knows about a service.

    ``expire`` always comes from somewhere: the panel when it answered, else the
    latest active order. ``status``, ``used_traffic`` and ``data_limit`` are the
    panel's and stay ``None`` when ``from_panel`` is false, so a caller can drop
    a usage line instead of printing a guess.
    """

    status: str | None
    expire: datetime | None
    used_traffic: int | None = None
    data_limit: int | None = None
    from_panel: bool = False


async def status_map_for_services(
    session: AsyncSession,
    client: PasarGuardClient,
    services: list[PasarGuardAccount],
    *,
    read_timeout: float | None = None,
    budget: float | None = None,
) -> dict[int, LiveStatus]:
    """Best-effort status/expire per account (panel when possible, else order expire).

    One ``get_user`` per row, never a raw failure to the caller: a row whose
    read fails keeps the expire hint and no status. The bot's lists and the web
    panel's pages both ask this, so the two show the same line.

    ``read_timeout`` caps one panel read and ``budget`` the whole loop (seconds);
    a row reached after the budget is spent skips the panel. Both are off by
    default, which is the bot's old behaviour.
    """
    result: dict[int, LiveStatus] = {}
    loop = asyncio.get_running_loop()
    deadline = None if budget is None else loop.time() + budget
    for svc in services:
        expire = await service_expire_hint(session, svc)
        live: UserResponse | None = None
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is None or remaining > 0:
            limit = read_timeout
            if remaining is not None:
                limit = remaining if limit is None else min(limit, remaining)
            try:
                call = client.get_user(svc.panel_username)
                live = await (call if limit is None else asyncio.wait_for(call, limit))
            except Exception:
                live = None
        if live is None:
            result[svc.id] = LiveStatus(status=None, expire=expire)
            continue
        if live.expire is not None:
            expire = normalize_expire(live.expire)
        result[svc.id] = LiveStatus(
            status=live.status.value if live.status is not None else None,
            expire=expire,
            used_traffic=live.used_traffic,
            data_limit=live.data_limit,
            from_panel=True,
        )
    return result
