"""Find a customer and see everything about them in one place.

The admin panel could only look up an **order id**. A customer who writes in
says «سرویسم کار نمی‌کنه» and gives their Telegram @username or nothing at all —
there was no way to get from that to their orders, services or wallet.

Manual wallet adjustment lives here too, because it must go through the same
immutable ledger as every automatic credit. An admin topping someone up by
hand is money, and money leaves a trail.
"""

from __future__ import annotations

import asyncio
import enum
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.texts import texts
from database.models import Order, OrderStatus, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.pasarguard_account import PasarGuardAccount
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.accounts import live_account_clause
from services.broadcast import expiring_users_exists
from services.customer_label import search_patterns
from services.store_catalog import CATEGORY_KIND_UNLIMITED, category_kind, category_trial_shape
from services.store_config import get_published_store_config
from services.ux_format import fa_toman
from services.wallet import WalletError, credit, debit

__all__ = [
    "CustomerCard",
    "CustomerServiceRow",
    "TrialMark",
    "UserFilter",
    "USER_FILTER_LABELS",
    "UserListPage",
    "UserRow",
    "list_users",
    "ManualAdjustError",
    "MANUAL_CREDIT_REASON",
    "MANUAL_DEBIT_REASON",
    "MAX_WALLET_TOMAN",
    "DIRECTION_ADD",
    "DIRECTION_DEDUCT",
    "WalletAdjustOutcome",
    "WalletAdjustPreview",
    "WalletAdjustRefused",
    "adjust_wallet_manually",
    "apply_wallet_adjustment",
    "parse_signed_toman",
    "preview_wallet_adjustment",
    "search_customer_rows",
    "count_customer_orders",
    "count_customer_referrals",
    "list_customer_orders",
    "list_customer_referrals",
    "reset_trial",
    "zero_wallet",
    "find_customers",
    "get_customer_card",
    "manual_adjust_key",
]

MANUAL_CREDIT_REASON = "admin_manual_credit"
MANUAL_DEBIT_REASON = "admin_manual_debit"

# One page of search results. More than this is a sign the query was too vague.
SEARCH_LIMIT = 8

# One page of the browse list. Ten rows plus navigation still fits a phone
# screen without the admin scrolling to reach the pager.
USER_PAGE_SIZE = 10


class ManualAdjustError(Exception):
    """A manual wallet change that must not be applied."""


@dataclass(frozen=True)
class CustomerServiceRow:
    account_id: int
    display_name: str
    panel_username: str
    subscription_url: str | None


@dataclass(frozen=True)
class TrialMark:
    """One store group's trial on the card: taken (with the date) or still open."""

    title: str
    taken_at: datetime | None
    legacy_consumed: bool = False


@dataclass(frozen=True)
class CustomerCard:
    """Everything the admin needs before answering a support message.

    Deliberately **not** a copy of the previous bot's card. Left out:
    «احراز هویت», «تایید شماره تلفن», «احراز عضویت کانال», «شماره کارت کاربر»
    and «محدودیت تغییر لوکیشن» — this bot has none of those concepts, and a
    field that always reads «—» teaches the reader to stop reading the card.

    Also left out: two different money totals with near-identical names. The
    old card showed «مبلغ کل پرداختی ۳۶۰٬۷۷۰» beside «جمع کل خرید ۱۸۰٬۰۰۰»
    with a zero balance, which cannot all be true at once. Every number here
    reconciles: `topped_up - spent ± manual adjustments == wallet_balance`.
    """

    user: User
    wallet_balance_toman: int
    services: list[CustomerServiceRow]
    orders_total: int
    orders_paid: int
    spent_toman: int
    last_order_at: datetime | None
    recent_ledger: list[WalletLedgerEntry]
    legacy_records_total: int = 0
    topped_up_toman: int = 0
    referral_count: int = 0
    referrer_label: str | None = None
    trial_claimed_at: datetime | None = None
    trials: list[TrialMark] | None = None

    @property
    def telegram_id(self) -> int:
        return int(self.user.telegram_id)

    @property
    def is_banned(self) -> bool:
        return bool(self.user.is_banned)

    @property
    def has_taken_trial(self) -> bool:
        return self.trial_claimed_at is not None or bool(self.user.legacy_trial_consumed)


def _normalize_query(raw: str) -> str:
    text = raw.strip()
    # Accept «@name», a bare name, a t.me link, or a numeric id.
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip()


_DIGITS_TO_ASCII = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)
_ORDER_REF = re.compile(r"^(?:#|سفارش|order)\s*#?\s*(\d{1,12})$", re.IGNORECASE)


def _order_ref(query: str) -> int | None:
    """«#123» or «سفارش ۱۲۳» → 123; anything else → None."""
    found = _ORDER_REF.match(query.strip().translate(_DIGITS_TO_ASCII))
    return int(found.group(1)) if found else None


def _sub_link_token(raw: str) -> str | None:
    """The last path segment of a pasted subscription link, or None if not one.

    A t.me link is a Telegram handle, not a subscription — `_normalize_query`
    already deals with it.
    """
    text = raw.strip()
    if "://" not in text or "t.me/" in text.lower():
        return None
    token = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return token if len(token) >= 6 else None


async def find_customers(session: AsyncSession, raw_query: str) -> list[User]:
    """Match what a customer actually sends: an id, a @username, a name, a
    referral code, the name of a config, a pasted subscription link, or «#123».

    Returns at most :data:`SEARCH_LIMIT` distinct users, newest first — a
    support request is nearly always about a recent customer.
    """
    query = _normalize_query(raw_query)
    if not query:
        return []

    stmt = select(User)
    order_ref = _order_ref(raw_query)
    token = _sub_link_token(raw_query)
    if order_ref is not None:
        stmt = stmt.where(User.id.in_(select(Order.user_id).where(Order.id == order_ref)))
    elif token is not None:
        stmt = stmt.where(
            User.id.in_(
                select(PasarGuardAccount.user_id).where(
                    PasarGuardAccount.subscription_url.like(f"%{token}"), live_account_clause()
                )
            )
        )
    elif query.isdigit():
        # A bare number is almost always a Telegram id; fall back to the id
        # column so an internal id typed from a log still resolves.
        stmt = stmt.where(or_(User.telegram_id == int(query), User.id == int(query)))
    else:
        pattern = f"%{query.lower()}%"
        account_hit = or_(
            *(
                func.lower(column).like(p)
                for p in search_patterns(query)
                for column in (
                    PasarGuardAccount.panel_username,
                    PasarGuardAccount.service_name,
                    PasarGuardAccount.display_name,
                )
            )
        )
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(pattern),
                func.lower(User.first_name).like(pattern),
                func.lower(User.last_name).like(pattern),
                func.lower(User.referral_code) == query.lower(),
                User.id.in_(
                    select(PasarGuardAccount.user_id).where(account_hit, live_account_clause())
                ),
            )
        )
    stmt = stmt.order_by(User.id.desc()).limit(SEARCH_LIMIT)
    return list((await session.execute(stmt)).scalars().all())


class UserFilter(str, enum.Enum):
    """Which slice of the user base to show.

    Modelled on the previous bot's filter list, minus two of its entries and
    plus two it lacked:

    - **dropped «موجودی منفی»**: this wallet cannot go negative. `debit()`
      refuses to overdraw, so the filter would be a button that always returns
      an empty list — worse than no button, because it teaches the owner the
      screen is broken. It becomes meaningful the day resellers get a credit
      line, and can come back then.
    - **dropped «شماره کارت فعال»**: per-user card numbers are a concept this
      bot does not have.
    - **added «مسدودها»**: the panel can ban, so it must be able to show who is
      banned. Nothing else lists them.
    - **added «بدون سرویس»**: the customers who signed up and never bought —
      the most useful audience there is, and the hardest to find by hand.
    """

    ALL = "all"
    WITH_BALANCE = "balance"
    WITH_REFERRALS = "refs"
    WITH_SERVICE = "svc"
    NO_SERVICE = "nosvc"
    BANNED = "banned"
    EXPIRING = "exp"


USER_FILTER_LABELS: dict[UserFilter, str] = {
    UserFilter.ALL: "همه",
    UserFilter.WITH_BALANCE: "موجودی‌دار",
    UserFilter.WITH_REFERRALS: "زیرمجموعه‌دار",
    UserFilter.WITH_SERVICE: "سرویس‌دار",
    UserFilter.NO_SERVICE: "بدون سرویس",
    UserFilter.BANNED: "مسدود",
    UserFilter.EXPIRING: "در حال انقضا",
}


def _filter_clause(user_filter: UserFilter) -> Any:
    """The WHERE for one filter, or None for «all».

    Written as correlated EXISTS rather than a join so a user with three
    services is still one row — a join would silently multiply the page.
    """
    has_service = (
        select(PasarGuardAccount.id)
        .where(PasarGuardAccount.user_id == User.id, live_account_clause())
        .exists()
    )
    referrer = aliased(User)
    clauses: dict[UserFilter, Any] = {
        UserFilter.WITH_BALANCE: User.wallet_balance_toman > 0,
        UserFilter.WITH_SERVICE: has_service,
        UserFilter.NO_SERVICE: ~has_service,
        UserFilter.BANNED: User.is_banned.is_(True),
        UserFilter.EXPIRING: expiring_users_exists(),
        UserFilter.WITH_REFERRALS: select(referrer.id)
        .where(referrer.referred_by_id == User.id)
        .exists(),
    }
    return clauses.get(user_filter)


@dataclass(frozen=True)
class UserRow:
    """One line of the browse list — enough to recognise someone without opening them."""

    user_id: int
    telegram_id: int
    label: str
    wallet_balance_toman: int
    service_count: int
    is_banned: bool
    is_admin: bool


@dataclass(frozen=True)
class UserListPage:
    rows: list[UserRow]
    offset: int
    total: int
    page_size: int = USER_PAGE_SIZE
    user_filter: UserFilter = UserFilter.ALL

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.rows) < self.total

    @property
    def prev_offset(self) -> int:
        """Step back by a full page, not by however many rows this page holds —
        the last page is usually partial, and stepping back by its length would
        leave a sliding window that never reaches the start."""
        return max(0, self.offset - self.page_size)

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.rows)


def _user_label(user: User) -> str:
    """A name an admin recognises, falling back through what Telegram gave us."""
    name = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


async def list_users(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = USER_PAGE_SIZE,
    user_filter: UserFilter = UserFilter.ALL,
) -> UserListPage:
    """One page of users, newest first.

    The section used to open straight into a search prompt, so an admin who did
    not already know a name or id had no way in at all — and no way to answer
    «چند تا کاربر داریم؟». Service counts come from one grouped query rather
    than a per-row lookup, so a page costs three queries regardless of size.
    """
    offset = max(0, int(offset))
    clause = _filter_clause(user_filter)

    count_stmt = select(func.count()).select_from(User)
    stmt = select(User)
    if clause is not None:
        count_stmt = count_stmt.where(clause)
        stmt = stmt.where(clause)

    # The total is the size of *this filter*, not of the whole user base —
    # «۳ نفر از ۲۶» on a filtered page would be two unrelated numbers.
    total = int(await session.scalar(count_stmt) or 0)
    stmt = stmt.order_by(User.id.desc()).offset(offset).limit(limit)
    users = list((await session.execute(stmt)).scalars().all())
    if not users:
        return UserListPage(
            rows=[], offset=offset, total=total, page_size=limit, user_filter=user_filter
        )
    rows = await _user_rows(session, users)
    return UserListPage(
        rows=rows, offset=offset, total=total, page_size=limit, user_filter=user_filter
    )


async def _user_rows(session: AsyncSession, users: list[User]) -> list[UserRow]:
    """The list rows for these users: service counts from one grouped query."""
    ids = [u.id for u in users]
    counts_result = await session.execute(
        select(PasarGuardAccount.user_id, func.count())
        .where(PasarGuardAccount.user_id.in_(ids), live_account_clause())
        .group_by(PasarGuardAccount.user_id)
    )
    counts = {int(uid): int(n) for uid, n in counts_result.all()}
    return [
        UserRow(
            user_id=int(u.id),
            telegram_id=int(u.telegram_id),
            label=_user_label(u),
            wallet_balance_toman=int(u.wallet_balance_toman or 0),
            service_count=counts.get(int(u.id), 0),
            is_banned=bool(u.is_banned),
            is_admin=bool(u.is_admin),
        )
        for u in users
    ]


async def search_customer_rows(session: AsyncSession, raw_query: str) -> list[UserRow]:
    """``find_customers`` as list rows, the same shape the browse list uses."""
    users = await find_customers(session, raw_query)
    return await _user_rows(session, users) if users else []


async def _unknown_legacy_trials(session: AsyncSession, user_id: int) -> list[Order]:
    """Live trial orders whose kind was never backfilled (kind NULL, plan ``trial-%``)."""
    result = await session.execute(
        select(Order)
        .join(Plan, Order.plan_id == Plan.id)
        .where(
            Order.user_id == user_id,
            Plan.name.like("trial-%"),
            Order.trial_kind.is_(None),
            Order.trial_voided_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def _trial_marks(session: AsyncSession, user: User) -> list[TrialMark]:
    """Per store group: taken (date) or open. Live personal trials only.

    A trial with no group (from before groups had trials) counts against the
    unlimited-kind group, as does ``trial_claimed_at`` with no trial order at
    all — what an import from the old bot leaves.
    """
    live = list(
        (
            await session.execute(
                select(Order).where(
                    Order.user_id == user.id,
                    Order.trial_kind == TRIAL_KIND_PERSONAL,
                    Order.trial_voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    cfg = await get_published_store_config(session)
    titles: dict[str, str] = {}
    unlimited_slugs: list[str] = []
    offered: list[str] = []
    if cfg is not None:
        by_id = {int(c.id): c for c in cfg.categories}
        for cat in cfg.categories:
            titles[str(cat.slug)] = str(cat.title_fa)
            parent = by_id.get(int(cat.parent_id)) if cat.parent_id is not None else None
            if category_trial_shape(cat) is None:
                continue
            offered.append(str(cat.slug))
            if (category_kind(cat) or category_kind(parent)) == CATEGORY_KIND_UNLIMITED:
                unlimited_slugs.append(str(cat.slug))
    taken: dict[str, datetime] = {}

    def _mark(slug: str, when: datetime) -> None:
        if slug not in taken or when < taken[slug]:
            taken[slug] = when

    # A trial from before groups had trials blocks EVERY unlimited-kind group
    # (``trial_open_for`` / ``counts_legacy``), so the card marks every one.
    for order in live:
        if order.trial_category_slug:
            _mark(str(order.trial_category_slug), order.created_at)
        else:
            for slug in unlimited_slugs:
                _mark(slug, order.created_at)
    unknown = await _unknown_legacy_trials(session, user.id)
    for order in unknown:
        for slug in unlimited_slugs:
            _mark(slug, order.created_at)
    if not live and not unknown and user.trial_claimed_at is not None:
        for slug in unlimited_slugs:
            _mark(slug, user.trial_claimed_at)
    legacy_slugs = set(unlimited_slugs) if user.legacy_trial_consumed else set()
    slugs = list(dict.fromkeys([*offered, *taken, *sorted(legacy_slugs)]))
    return [
        TrialMark(
            title=titles.get(slug, slug),
            taken_at=taken.get(slug),
            legacy_consumed=slug in legacy_slugs,
        )
        for slug in slugs
    ]


async def get_customer_card(session: AsyncSession, user_id: int) -> CustomerCard:
    """Wallet, services, order history and recent ledger for one customer."""
    user = await session.get(User, user_id)
    if user is None:
        raise ManualAdjustError("کاربر یافت نشد.")

    accounts = list(
        (
            await session.execute(
                select(PasarGuardAccount)
                .where(PasarGuardAccount.user_id == user_id, live_account_clause())
                .order_by(PasarGuardAccount.id.desc())
            )
        )
        .scalars()
        .all()
    )
    services = [
        CustomerServiceRow(
            account_id=int(a.id),
            display_name=str(a.display_name or a.service_name or a.panel_username),
            panel_username=str(a.panel_username),
            subscription_url=a.subscription_url,
        )
        for a in accounts
    ]

    paid_statuses = (
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISIONING,
        OrderStatus.ACTIVE,
    )
    totals = (
        await session.execute(
            select(
                func.count(Order.id),
                func.max(Order.created_at),
            ).where(Order.user_id == user_id)
        )
    ).one()
    paid = (
        await session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.price_toman), 0),
            ).where(Order.user_id == user_id, Order.status.in_(paid_statuses))
        )
    ).one()

    ledger = list(
        (
            await session.execute(
                select(WalletLedgerEntry)
                .where(WalletLedgerEntry.user_id == user_id)
                .order_by(WalletLedgerEntry.id.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )

    # Everything that ever went *into* the wallet, so the admin can answer
    # «چقدر به ما داده؟» without adding up the ledger by eye.
    topped_up = int(
        await session.scalar(
            select(func.coalesce(func.sum(WalletLedgerEntry.amount_toman), 0)).where(
                WalletLedgerEntry.user_id == user_id,
                WalletLedgerEntry.entry_type == WalletEntryType.credit,
            )
        )
        or 0
    )

    referral_count = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.referred_by_id == user_id)
        )
        or 0
    )
    referrer_label: str | None = None
    if user.referred_by_id:
        referrer = await session.get(User, user.referred_by_id)
        if referrer is not None:
            referrer_label = (
                f"@{referrer.username}" if referrer.username else str(referrer.telegram_id)
            )

    from database.models.mirza_import import MirzaRecord

    legacy_records_total = int(
        await session.scalar(
            select(func.count())
            .select_from(MirzaRecord)
            .where(MirzaRecord.target_user_id == user_id)
        )
        or 0
    )
    return CustomerCard(
        legacy_records_total=legacy_records_total,
        user=user,
        wallet_balance_toman=int(user.wallet_balance_toman or 0),
        services=services,
        orders_total=int(totals[0] or 0),
        orders_paid=int(paid[0] or 0),
        spent_toman=int(paid[1] or 0),
        last_order_at=totals[1],
        recent_ledger=ledger,
        topped_up_toman=topped_up,
        referral_count=referral_count,
        referrer_label=referrer_label,
        trial_claimed_at=user.trial_claimed_at,
        trials=await _trial_marks(session, user),
    )


async def list_customer_orders(
    session: AsyncSession, user_id: int, *, limit: int = 10, offset: int = 0
) -> list[Order]:
    """This customer's orders, newest first — the answer to «چی خریده؟»."""
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.id.desc())
        .offset(max(0, offset))
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_customer_orders(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
        )
        or 0
    )


async def list_customer_referrals(
    session: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0
) -> list[User]:
    """Everyone who arrived on this customer's invite link."""
    result = await session.execute(
        select(User)
        .where(User.referred_by_id == user_id)
        .order_by(User.id.desc())
        .offset(max(0, offset))
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_customer_referrals(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.referred_by_id == user_id)
        )
        or 0
    )


async def reset_trial(session: AsyncSession, user_id: int) -> User:
    """Let someone take every group's free trial again.

    Voids their live personal trials rather than deleting them: the order and
    the panel account are history, and the account may still be live. Clearing
    ``trial_claimed_at`` alone once looked like a reset and did nothing — the
    old order still matched and the next tap resumed it.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise ManualAdjustError("کاربر یافت نشد.")
    live = (
        (
            await session.execute(
                select(Order).where(
                    Order.user_id == user_id,
                    Order.trial_kind == TRIAL_KIND_PERSONAL,
                    Order.trial_voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # A legacy trial of unknown kind blocks the direct group just the same
    # (``_has_unknown_legacy_trial``), so a reset has to void it too.
    live = [*live, *await _unknown_legacy_trials(session, user_id)]
    if not live and user.trial_claimed_at is None and not user.legacy_trial_consumed:
        raise ManualAdjustError("این کاربر تست رایگان نگرفته.")
    now = datetime.now(UTC)
    for order in live:
        order.trial_voided_at = now
    user.trial_claimed_at = None
    user.legacy_trial_consumed = False
    await session.flush()
    return user


async def zero_wallet(
    session: AsyncSession,
    *,
    user_id: int,
    admin_telegram_id: int,
) -> int:
    """Take the whole balance to zero, through the ledger like any other change.

    Returns the amount removed. Money never moves without a row explaining it,
    so this is a normal debit rather than a write straight to the balance.
    """
    user = await session.get(User, user_id)
    if user is None:
        raise ManualAdjustError("کاربر یافت نشد.")
    balance = int(user.wallet_balance_toman or 0)
    if balance <= 0:
        raise ManualAdjustError("کیف پول این کاربر خالی است.")
    await debit(
        session,
        user_id=user_id,
        amount_toman=balance,
        reason=MANUAL_DEBIT_REASON,
        idempotency_key=manual_adjust_key(
            admin_telegram_id=admin_telegram_id,
            user_id=user_id,
            stamp=f"zero:{balance}:{datetime.now(UTC).isoformat(timespec='seconds')}",
        ),
    )
    return balance


def manual_adjust_key(*, admin_telegram_id: int, user_id: int, stamp: str) -> str:
    """Idempotency key for one manual adjustment.

    ``stamp`` makes a deliberate second top-up of the same amount possible while
    a double-tap on the same confirmation stays a single credit.
    """
    return f"admin_adjust:{admin_telegram_id}:{user_id}:{stamp}"


async def _entry_by_key(session: AsyncSession, key: str) -> WalletLedgerEntry | None:
    return (
        await session.execute(
            select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key == key)
        )
    ).scalar_one_or_none()


async def _adjust(
    session: AsyncSession,
    *,
    user_id: int,
    amount_toman: int,
    admin_telegram_id: int,
    stamp: str,
    note: str | None,
) -> tuple[WalletLedgerEntry, bool]:
    """The body of a manual adjustment: ``(entry, created)``. See ``adjust_wallet_manually``."""
    if amount_toman == 0:
        raise ManualAdjustError("مبلغ صفر است.")

    user = await session.get(User, user_id)
    if user is None:
        raise ManualAdjustError("کاربر یافت نشد.")

    key = manual_adjust_key(admin_telegram_id=admin_telegram_id, user_id=user_id, stamp=stamp)
    # A repeat of a request that already landed answers with its entry. Looking
    # first matters for a deduction: once it has landed, the balance may no
    # longer cover it, and the overdraft check below would call the replay a
    # refusal although the money moved exactly once.
    replay = await _entry_by_key(session, key)
    if replay is not None:
        return replay, False

    trail = f"admin={admin_telegram_id}"
    if note:
        trail = f"{trail} note={note}"

    try:
        if amount_toman > 0:
            if int(user.wallet_balance_toman or 0) + amount_toman > MAX_WALLET_TOMAN:
                raise ManualAdjustError(texts.admin_panel_invalid)
            return await credit(
                session,
                user_id=user_id,
                amount_toman=amount_toman,
                reason=MANUAL_CREDIT_REASON,
                idempotency_key=key,
                note=trail,
            )
        balance = int(user.wallet_balance_toman or 0)
        if balance < -amount_toman:
            raise ManualAdjustError(
                f"موجودی کافی نیست. موجودی فعلی: {balance:,} تومان".replace(",", "٬")
            )
        return await debit(
            session,
            user_id=user_id,
            amount_toman=-amount_toman,
            reason=MANUAL_DEBIT_REASON,
            idempotency_key=key,
            note=trail,
        )
    except WalletError as exc:
        raise ManualAdjustError(f"تغییر کیف پول انجام نشد: {exc}") from exc


async def adjust_wallet_manually(
    session: AsyncSession,
    *,
    user_id: int,
    amount_toman: int,
    admin_telegram_id: int,
    stamp: str,
    note: str | None = None,
) -> WalletLedgerEntry:
    """Top up or deduct by hand, through the normal ledger.

    A positive amount credits, a negative amount debits. Debiting more than the
    balance is refused rather than clamped: a negative wallet would be a silent
    debt the customer never agreed to. The caller commits. The same ``stamp``
    from the same admin for the same customer is the same adjustment: a repeat
    returns the first entry and writes nothing.
    """
    entry, _ = await _adjust(
        session,
        user_id=user_id,
        amount_toman=amount_toman,
        admin_telegram_id=admin_telegram_id,
        stamp=stamp,
        note=note,
    )
    return entry


# --------------------------------------------------------------------------- #
# The wallet form: shared by the bot's typed amount and the panel's form.
# --------------------------------------------------------------------------- #

# The balance column is a 64-bit integer; a bigger number is a typo, and the
# database would raise an OverflowError rather than refuse it in words.
MAX_WALLET_TOMAN = 2**63 - 1

_BIDI_MARKS = ("\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u2066", "\u2067", "\u2069")


def parse_signed_toman(raw: str) -> int | None:
    """«50000», «-20000», «۵۰٬۰۰۰» → an int. None when the text is not a number.

    Returning None keeps the raw Python ValueError text out of the Persian UI.
    A number beyond 64 bits is not a number the wallet can hold: None as well.
    """
    # Copied ids carry bidi marks and a Unicode minus from RTL chats.
    for mark in _BIDI_MARKS:
        raw = raw.replace(mark, "")
    raw = raw.replace("\u2212", "-").replace(" ", "")
    text = raw.strip().replace(",", "").replace("٬", "").replace("،", "")
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    negative = text.startswith("-")
    digits = text[1:] if negative or text.startswith("+") else text
    if not digits.isdecimal() or len(digits) > 19:
        return None
    value = int(digits)
    if value > MAX_WALLET_TOMAN:
        return None
    return -value if negative else value


class WalletAdjustRefused(ManualAdjustError):
    """A wallet form the admin must fix. ``str(exc)`` is the bot's own refusal text.

    ``kind`` is ``invalid``, ``user_not_found`` or ``insufficient``; the bot
    keeps the person on the same step for the first and last, and ends it for
    the second.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class WalletAdjustPreview:
    """What the confirm step states: who, how much (signed), before and after."""

    user: User
    amount_toman: int
    balance_toman: int

    @property
    def after_toman(self) -> int:
        return self.balance_toman + self.amount_toman

    @property
    def is_credit(self) -> bool:
        return self.amount_toman > 0


DIRECTION_ADD = "add"
DIRECTION_DEDUCT = "deduct"


async def preview_wallet_adjustment(
    session: AsyncSession,
    *,
    user_id: int,
    raw_amount: str,
    direction: str | None = None,
) -> WalletAdjustPreview:
    """Check what an admin typed before the confirm step; nothing is written.

    The bot's amount is signed text («-20000» deducts), so ``direction`` stays
    ``None``. The panel's form has a direction beside an unsigned amount: it
    passes ``"add"`` or ``"deduct"`` and a minus sign in the field is refused,
    so the two can never disagree about which way the money goes.
    """
    amount = parse_signed_toman(raw_amount or "")
    if direction is not None:
        if direction not in (DIRECTION_ADD, DIRECTION_DEDUCT) or (amount or 0) <= 0:
            amount = None
        elif direction == DIRECTION_DEDUCT:
            amount = -int(amount or 0)
    if amount is None or amount == 0:
        raise WalletAdjustRefused("invalid", texts.admin_panel_invalid)
    user = await session.get(User, user_id)
    if user is None:
        raise WalletAdjustRefused("user_not_found", texts.admin_user_not_found)
    balance = int(user.wallet_balance_toman or 0)
    if amount < 0 and balance < -amount:
        raise WalletAdjustRefused(
            "insufficient", texts.admin_wallet_insufficient.format(wallet=fa_toman(balance))
        )
    if amount > 0 and balance + amount > MAX_WALLET_TOMAN:
        raise WalletAdjustRefused("invalid", texts.admin_panel_invalid)
    return WalletAdjustPreview(user=user, amount_toman=amount, balance_toman=balance)


@dataclass(frozen=True)
class WalletAdjustOutcome:
    entry: WalletLedgerEntry
    created: bool  # False: this request had already landed, nothing was written now

    @property
    def balance_after_toman(self) -> int:
        return int(self.entry.balance_after_toman)


async def apply_wallet_adjustment(
    session: AsyncSession,
    *,
    user_id: int,
    amount_toman: int,
    admin_telegram_id: int,
    stamp: str,
) -> WalletAdjustOutcome:
    """Apply one confirmed adjustment and commit: ledger entry, balance and report together.

    ``adjust_wallet_manually`` plus the commit, and one more answer: when two
    deliveries of the same request race, the loser hits the ledger's unique key
    (or a busy database) and rolls back; if the winner's entry is there, that is
    this request's answer and not an error. SQLite can reject the losing write
    before the winner commits; retry only that busy case on a fresh transaction.
    Other errors are raised without replaying a financial operation.
    """
    key = manual_adjust_key(admin_telegram_id=admin_telegram_id, user_id=user_id, stamp=stamp)
    attempt = 0
    while True:
        try:
            entry, created = await _adjust(
                session,
                user_id=user_id,
                amount_toman=amount_toman,
                admin_telegram_id=admin_telegram_id,
                stamp=stamp,
                note=None,
            )
            await session.commit()
        except (IntegrityError, OperationalError) as exc:
            await session.rollback()
            landed = await _entry_by_key(session, key)
            if landed is not None:
                return WalletAdjustOutcome(entry=landed, created=False)
            busy = (
                isinstance(exc, OperationalError)
                and session.get_bind().dialect.name == "sqlite"
                and (getattr(exc.orig, "sqlite_errorcode", 0) or 0) & 0xFF
                in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            )
            # The empty replay lookup pinned a new read snapshot too. Release
            # it before yielding, otherwise the winner stays invisible.
            await session.rollback()
            if not busy or attempt >= 5:
                raise
            await asyncio.sleep(0.05 * 2**attempt)
            attempt += 1
            continue
        except BaseException:
            await session.rollback()
            raise
        return WalletAdjustOutcome(entry=entry, created=created)
