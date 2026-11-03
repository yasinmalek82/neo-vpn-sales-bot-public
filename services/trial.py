"""Free trial issuance: one per person per store group, idempotent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api_client import PasarGuardClient
from api_client.exceptions import PasarGuardError, PasarGuardNotFoundError
from bot.config import Settings
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL, TRIAL_KIND_RESELLER
from database.models.plan import DataLimitResetStrategy
from services.customer_label import split_label
from services.orders import trial_fingerprint
from services.panel_settings import resolve_groups_for_plan_type
from services.pricing import PlanType
from services.provisioning import ProvisionError, provision_order
from services.report_events import trial_issued
from services.report_outbox import enqueue_report
from services.store_catalog import (
    CATEGORY_KIND_UNLIMITED,
    category_kind,
    category_reseller_trial,
    category_trial_shape,
)
from services.store_config import get_published_store_config
from services.ux_format import DEFAULT_DISPLAY_TIMEZONE


class TrialError(Exception):
    """Business-rule failure for trial operations.

    ``order_id`` gives a handler a durable reseller operation to settle or
    offer for explicit recovery after panel I/O.  Labels and FSM state are UX
    only; neither identifies a giveaway.
    """

    def __init__(
        self,
        code: str,
        *,
        order_id: int | None = None,
        original_exception: BaseException | None = None,
    ) -> None:
        super().__init__(code)
        self.order_id = order_id
        self.original_exception = original_exception


@dataclass(frozen=True)
class TrialResult:
    order: Order
    account: PasarGuardAccount
    already_claimed: bool
    delivery_data_limit_gb: int | None = None
    delivery_duration_days: int | None = None


def trial_plan_name(*, data_limit_gb: int, duration_days: int) -> str:
    return f"trial-{data_limit_gb}GB-{duration_days}d"


async def _get_or_create_trial_plan(
    session: AsyncSession,
    *,
    data_limit_gb: int,
    duration_days: int,
    hwid_limit: int,
) -> Plan:
    """Trial Plan stores empty group_ids; provision uses caller default_group_ids.

    Callers pass the groups of the store group the trial belongs to.
    """
    name = trial_plan_name(data_limit_gb=data_limit_gb, duration_days=duration_days)
    result = await session.execute(select(Plan).where(Plan.name == name))
    plan = result.scalar_one_or_none()
    if plan is None:
        plan = Plan(
            name=name,
            description=f"Free trial {data_limit_gb}GB / {duration_days} days",
            data_limit_gb=data_limit_gb,
            duration_days=duration_days,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=hwid_limit,
            is_active=True,
            sort_order=0,
        )
        session.add(plan)
        await session.flush()
    else:
        plan.data_limit_gb = data_limit_gb
        plan.duration_days = duration_days
        plan.hwid_limit = hwid_limit
        plan.group_ids = []
        plan.price_toman = 0
        plan.is_active = True
    return plan


@dataclass(frozen=True)
class TrialOffer:
    """One store group's free trial, resolved against the live catalog."""

    slug: str
    title_fa: str
    data_limit_gb: int
    duration_days: int
    group_ids: list[int]
    # A trial issued before groups had trials was a direct one; it counts
    # against the unlimited (direct) groups and nothing else.
    counts_legacy: bool


def _open_for_sale(cat: object) -> bool:
    if not getattr(cat, "enabled", False):
        return False
    return bool(dict(getattr(cat, "sales_flags", None) or {}).get("allow_new_sale", True))


async def list_trial_offers(
    session: AsyncSession, settings: Settings, *, reseller: bool = False
) -> list[TrialOffer]:
    """Every group a trial can be taken from, in menu order.

    A group closed for sale offers no trial either — and neither does a child
    of a closed parent, which the purchase menu cannot reach. ``reseller=True``
    keeps only the groups whose «تست نماینده» switch is on.
    """
    cfg = await get_published_store_config(session)
    if cfg is None:
        return []
    by_id = {int(c.id): c for c in cfg.categories}
    offers: list[TrialOffer] = []
    for cat in cfg.categories:
        shape = category_trial_shape(cat)
        if shape is None or not _open_for_sale(cat):
            continue
        if reseller and not category_reseller_trial(cat):
            continue
        parent = by_id.get(int(cat.parent_id)) if cat.parent_id is not None else None
        if cat.parent_id is not None and (parent is None or not _open_for_sale(parent)):
            continue
        kind = category_kind(cat) or category_kind(parent)
        unlimited = kind == CATEGORY_KIND_UNLIMITED
        groups = await resolve_groups_for_plan_type(
            session,
            settings,
            PlanType.UNLIMITED if unlimited else PlanType.VOLUME,
            store_category_id=int(cat.id),
        )
        if not groups:
            # A trial on no group would create a user with no inbounds.
            continue
        offers.append(
            TrialOffer(
                slug=str(cat.slug),
                title_fa=str(cat.title_fa),
                data_limit_gb=shape[0],
                duration_days=shape[1],
                group_ids=list(groups),
                counts_legacy=unlimited,
            )
        )
    return offers


async def trial_open_for(session: AsyncSession, user: User, offer: TrialOffer) -> bool:
    """Whether ``user`` may still take ``offer``'s trial.

    One per person per group. Having paid for a service is not a reason to
    have forfeited a trial never taken — that rule once hid the trial from
    every customer who had bought anything.
    """
    if await _load_personal_trial_orders(session, user_id=user.id, slug=offer.slug):
        return False
    if offer.counts_legacy and await _legacy_trial_taken(session, user):
        return False
    return True


async def user_trial_eligible(session: AsyncSession, settings: Settings, user: User) -> bool:
    """Whether any group still has a trial this user may take."""
    for offer in await list_trial_offers(session, settings):
        if await trial_open_for(session, user, offer):
            return True
    return False


async def _legacy_trial_taken(session: AsyncSession, user: User) -> bool:
    """A trial from before groups had trials, which counts as the direct one.

    Three shapes: a personal order with no group; a trial order whose kind was
    never safely backfilled (guessing it was not personal would mint a second
    entitlement); or ``trial_claimed_at`` with no trial order at all, which is
    what an import from the old bot leaves behind.
    """
    if user.legacy_trial_consumed:
        return True
    if await _load_personal_trial_orders(session, user_id=user.id, slug=None):
        return True
    if await _has_unknown_legacy_trial(session, user_id=user.id):
        return True
    if user.trial_claimed_at is None:
        return False
    any_personal = await session.scalar(
        select(Order.id)
        .where(Order.user_id == user.id, Order.trial_kind == TRIAL_KIND_PERSONAL)
        .limit(1)
    )
    return any_personal is None


async def issue_free_trial(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    user_id: int,
    category_slug: str,
    counts_legacy: bool,
    data_limit_gb: int,
    duration_days: int,
    hwid_limit: int,
    group_ids: list[int],
    enabled: bool = True,
) -> TrialResult:
    """Create a zero-price trial order for one store group and provision it.

    Idempotent per group: a second call returns that group's existing trial.
    ``counts_legacy`` (from ``TrialOffer``) makes a pre-group trial count as
    this group's — it was a direct trial, and the direct groups are unlimited.
    ``users.trial_claimed_at`` is kept as «last trial taken» for the admin card.
    """
    if not enabled:
        raise TrialError("trial_disabled")

    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if user is None:
        raise TrialError("user_not_found")

    personal_orders = await _load_personal_trial_orders(
        session, user_id=user.id, slug=category_slug
    )
    if len(personal_orders) > 1:
        raise TrialError("trial_recovery_unsafe")
    if personal_orders:
        return await _resume_personal_trial(session, client, order=personal_orders[0])

    if counts_legacy:
        legacy = await _load_personal_trial_orders(session, user_id=user.id, slug=None)
        if len(legacy) == 1:
            return await _resume_personal_trial(session, client, order=legacy[0])
        if legacy or await _has_unknown_legacy_trial(session, user_id=user.id):
            raise TrialError("trial_recovery_unsafe")
        if await _legacy_trial_taken(session, user):
            raise TrialError("trial_already_claimed")

    # group_ids arg is the provision default; Plan itself stores [].
    # Callers resolve the trial panel group, then the volume group, then the default.
    plan = await _get_or_create_trial_plan(
        session,
        data_limit_gb=data_limit_gb,
        duration_days=duration_days,
        hwid_limit=hwid_limit,
    )
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_APPROVED,
        price_toman=0,
        discount_toman=0,
        force_new=True,
        trial_kind=TRIAL_KIND_PERSONAL,
        trial_category_slug=category_slug,
        delivery_data_limit_gb=data_limit_gb,
        delivery_duration_days=duration_days,
        delivery_hwid_limit=hwid_limit,
        delivery_group_ids=list(group_ids),
        paid_at=datetime.now(UTC),
    )
    try:
        async with session.begin_nested():
            session.add(order)
            user.trial_claimed_at = datetime.now(UTC)
            await session.flush()
    except IntegrityError as exc:
        if order in session:
            session.expunge(order)
        await session.refresh(user)
        collided = await _load_personal_trial_orders(session, user_id=user.id, slug=category_slug)
        if len(collided) == 1:
            return await _resume_personal_trial(session, client, order=collided[0])
        raise TrialError("trial_creation_conflict") from exc

    try:
        account, _reward = await provision_order(
            session,
            client,
            order_id=order.id,
            default_group_ids=group_ids,
        )
    except ProvisionError as exc:
        raise TrialError(str(exc)) from exc

    # Distinct trial fingerprint so paid renew never extends trial.
    account.plan_fingerprint = trial_fingerprint(
        data_limit_gb=data_limit_gb, duration_days=duration_days
    )
    await session.flush()
    await session.refresh(order)
    return _trial_result(order=order, account=account, already_claimed=False, plan=plan)


async def enqueue_trial_report(session: AsyncSession, order: Order) -> None:
    """Trials are the top of the funnel; the owner wants to watch the rate.

    Called once the panel username is stamped, so the card can name the config.
    Every trial is keyed on its order. A personal trial was once keyed on the
    telegram id («one ever»); with one trial per store group, that key reported
    the first group's trial and swallowed the second.
    """
    user = await session.get(User, int(order.user_id))
    if user is None or order.trial_kind is None:
        return
    plan = await session.get(Plan, int(order.plan_id)) if order.plan_id else None
    if order.delivery_data_limit_gb is not None:
        gb = int(order.delivery_data_limit_gb)
    elif plan is not None:
        gb = int(plan.data_limit_gb or 0)
    else:
        gb = 0
    if order.delivery_duration_days is not None:
        days = int(order.delivery_duration_days)
    elif plan is not None:
        days = int(plan.duration_days or 0)
    else:
        days = 0
    report = trial_issued(
        telegram_id=int(user.telegram_id),
        name=user.first_name or user.username,
        username=user.username,
        is_reseller=bool(user.is_reseller),
        gb=gb,
        days=days,
        order_id=int(order.id),
        service_name=order.service_name,
        phone=split_label(order.customer_label).phone,
        panel_username=order.panel_username,
        when=order.paid_at or order.created_at,
        link_user_id=int(user.id),
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def _load_personal_trial_orders(
    session: AsyncSession, *, user_id: int, slug: str | None
) -> list[Order]:
    """Live (not reset) personal trials of one group; ``slug=None`` = pre-group."""
    result = await session.execute(
        select(Order)
        .join(Plan, Order.plan_id == Plan.id)
        .where(
            Order.user_id == user_id,
            Order.trial_kind == TRIAL_KIND_PERSONAL,
            Order.trial_voided_at.is_(None),
            (
                Order.trial_category_slug.is_(None)
                if slug is None
                else Order.trial_category_slug == slug
            ),
        )
        .options(selectinload(Order.plan))
    )
    return list(result.scalars().all())


async def _has_unknown_legacy_trial(session: AsyncSession, *, user_id: int) -> bool:
    return (
        await session.scalar(
            select(Order.id)
            .join(Plan, Order.plan_id == Plan.id)
            .where(
                Order.user_id == user_id,
                Plan.name.like("trial-%"),
                Order.trial_kind.is_(None),
                Order.trial_voided_at.is_(None),
            )
            .limit(1)
        )
    ) is not None


def _trial_result(
    *, order: Order, account: PasarGuardAccount, already_claimed: bool, plan: Plan
) -> TrialResult:
    _ = plan
    data_limit_gb, duration_days = _trial_snapshot(order)
    return TrialResult(
        order=order,
        account=account,
        already_claimed=already_claimed,
        delivery_data_limit_gb=data_limit_gb,
        delivery_duration_days=duration_days,
    )


def _has_complete_trial_snapshot(order: Order) -> bool:
    group_ids = order.delivery_group_ids
    return (
        order.delivery_data_limit_gb is not None
        and int(order.delivery_data_limit_gb) > 0
        and order.delivery_duration_days is not None
        and int(order.delivery_duration_days) > 0
        and order.delivery_hwid_limit is not None
        and int(order.delivery_hwid_limit) > 0
        and isinstance(group_ids, list)
        and all(isinstance(group_id, int) for group_id in group_ids)
    )


def _trial_snapshot(order: Order) -> tuple[int, int]:
    if not _has_complete_trial_snapshot(order):
        raise TrialError("trial_recovery_unsafe")
    assert order.delivery_data_limit_gb is not None
    assert order.delivery_duration_days is not None
    return int(order.delivery_data_limit_gb), int(order.delivery_duration_days)


async def _resume_personal_trial(
    session: AsyncSession, client: PasarGuardClient, *, order: Order
) -> TrialResult:
    username = (order.panel_username or "").strip()
    if not username:
        raise TrialError("trial_recovery_unsafe")
    if order.status == OrderStatus.ACTIVE:
        account = await session.scalar(
            select(PasarGuardAccount).where(
                PasarGuardAccount.user_id == order.user_id,
                PasarGuardAccount.panel_username == username,
            )
        )
        if account is None:
            raise TrialError("trial_recovery_unsafe")
        try:
            await client.get_user(username)
        except PasarGuardNotFoundError as exc:
            raise TrialError("trial_panel_missing") from exc
        except PasarGuardError as exc:
            raise TrialError("trial_panel_unreachable") from exc
        return TrialResult(order=order, account=account, already_claimed=True)
    if order.status not in {OrderStatus.PROVISIONING, OrderStatus.PROVISION_FAILED}:
        raise TrialError("trial_already_claimed")
    plan = order.plan
    if plan is None:
        raise TrialError("trial_recovery_unsafe")
    data_limit_gb, duration_days = _trial_snapshot(order)

    try:
        account, _reward = await provision_order(session, client, order_id=order.id)
    except ProvisionError as exc:
        raise TrialError(str(exc)) from exc
    account.plan_fingerprint = trial_fingerprint(
        data_limit_gb=data_limit_gb,
        duration_days=duration_days,
    )
    await session.flush()
    await session.refresh(order)
    return _trial_result(order=order, account=account, already_claimed=False, plan=plan)


# --------------------------------------------------------------------------- #
# Reseller trials
#
# A different operation from the customer trial, not a variation of it. A
# customer takes one trial ever, for themselves. A reseller hands out many —
# one per prospective customer walking into the shop — free up to a monthly
# quota and charged after that. So `trial_claimed_at` is never touched here:
# that flag is about the reseller's own personal trial, which they may still
# take as a customer.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResellerTrialQuota:
    used: int
    quota: int
    price_toman: int
    period_label: str

    @property
    def remaining(self) -> int:
        return max(0, self.quota - self.used)

    @property
    def is_free(self) -> bool:
        return self.used < self.quota

    @property
    def can_issue(self) -> bool:
        """Free quota left, or a price set for the extras.

        With the quota spent and no price configured there is no honest answer
        to «چند بدم؟», so the bot says so instead of guessing at zero.
        """
        return self.is_free or self.price_toman > 0


def _jalali_month_start(now: datetime | None = None) -> datetime:
    """First instant of the current Jalali month, in UTC.

    Jalali rather than Gregorian because «سهمیهٔ ماهانه» is counted by the owner
    in the calendar he actually uses; a quota that resets on the 1st of a month
    he never looks at would appear to reset at random.
    """
    import jdatetime

    tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
    local = (now or datetime.now(UTC)).astimezone(tz)
    jnow = jdatetime.datetime.fromgregorian(datetime=local)
    jstart = jdatetime.datetime(jnow.year, jnow.month, 1, tzinfo=tz)
    return jstart.togregorian().astimezone(UTC)


async def get_reseller_trial_quota(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> ResellerTrialQuota:
    """How many trials this reseller has handed out this Jalali month.

    Counted from the orders themselves rather than a counter column, so it
    cannot drift away from what actually happened — and a reset or a manual
    fix to the orders is reflected without anything else to remember.
    """
    from services.reseller import get_rate

    start = _jalali_month_start(now)
    used = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .join(Plan, Order.plan_id == Plan.id)
            .where(
                Order.user_id == user_id,
                Plan.name.like("trial-%"),
                (Order.trial_kind == TRIAL_KIND_RESELLER) | Order.trial_kind.is_(None),
                # Only giveaways that reached a service (ACTIVE, or EXPIRED after
                # one did) or are still in flight: a panel outage must not burn
                # the month's free ones.
                Order.status.in_(
                    (
                        OrderStatus.ACTIVE,
                        OrderStatus.EXPIRED,
                        OrderStatus.PAYMENT_APPROVED,
                        OrderStatus.PROVISIONING,
                    )
                ),
                Order.created_at >= start,
            )
        )
        or 0
    )
    rate = await get_rate(session, user_id)
    import jdatetime

    jstart = jdatetime.datetime.fromgregorian(
        datetime=start.astimezone(ZoneInfo(DEFAULT_DISPLAY_TIMEZONE))
    )
    return ResellerTrialQuota(
        used=used,
        quota=int(getattr(rate, "trial_quota_monthly", 0) or 0),
        price_toman=int(getattr(rate, "trial_price_toman", 0) or 0),
        period_label=jstart.strftime("%Y/%m"),
    )


async def reseller_month_sales(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> int:
    """Sales this Jalali month, counted the way «💰 گزارش مالی» counts a sale.

    Same month as the trial quota beside it on the resellers list, and the
    same rule as the finance windows: money moved (a paid trial counts, a free
    one does not) and the service exists.
    """
    from services.reporting import EARNING_STATUSES

    start = _jalali_month_start(now)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == user_id,
                Order.status.in_(EARNING_STATUSES),
                Order.price_toman > 0,
                Order.activated_at.is_not(None),
                Order.activated_at >= start,
            )
        )
        or 0
    )


async def _lock_reseller(session: AsyncSession, user_id: int) -> User:
    """Serialize giveaways for one shop until this transaction commits.

    PostgreSQL locks the row with ``FOR UPDATE``. SQLite ignores that clause,
    and a read-then-write in the waiting transaction raises immediately, so the
    first statement on SQLite is the row write that blocks until the holder
    commits.
    """
    if session.sync_session.get_bind().dialect.name == "sqlite":
        result = await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise TrialError("user_not_found")
        user = await session.get(User, user_id)
        if user is None:
            raise TrialError("user_not_found")
        return user
    locked = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = locked.scalar_one_or_none()
    if user is None:
        raise TrialError("user_not_found")
    return user


async def issue_reseller_trial(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    user_id: int,
    data_limit_gb: int,
    duration_days: int,
    hwid_limit: int,
    group_ids: list[int],
    display_name: str | None = None,
    service_name: str | None = None,
    category_slug: str | None = None,
    enabled: bool = True,
) -> TrialResult:
    """Hand out one trial service for a reseller to give away.

    ``service_name`` is the customer's Latin name, and it seeds the panel
    username the same way a purchase does — a shop that has to fix something in
    PasarGuard looks for `reza_a7f3`, not for the third `neo_…` of the day.
    ``display_name`` is the full label, phone included, and stays out of it.

    Charging happens **after** the order exists and is keyed on its id, so a
    retry after a timeout cannot debit twice for the same trial — the same
    absolute-write discipline the panel calls use.
    """
    from services.wallet import InsufficientWalletFunds, debit

    if not enabled:
        raise TrialError("trial_disabled")

    user = await _lock_reseller(session, user_id)
    if not user.is_reseller:
        raise TrialError("not_a_reseller")

    quota = await get_reseller_trial_quota(session, user_id)
    if not quota.can_issue:
        raise TrialError("trial_quota_exhausted")
    charge = 0 if quota.is_free else quota.price_toman

    plan = await _get_or_create_trial_plan(
        session,
        data_limit_gb=data_limit_gb,
        duration_days=duration_days,
        hwid_limit=hwid_limit,
    )
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_APPROVED,
        price_toman=charge,
        discount_toman=0,
        service_name=service_name,
        customer_label=display_name,
        force_new=True,
        trial_kind=TRIAL_KIND_RESELLER,
        trial_category_slug=category_slug,
        delivery_data_limit_gb=data_limit_gb,
        delivery_duration_days=duration_days,
        delivery_hwid_limit=hwid_limit,
        delivery_group_ids=list(group_ids),
        paid_at=datetime.now(UTC),
    )
    session.add(order)
    await session.flush()

    if charge > 0:
        try:
            await debit(
                session,
                user_id=user.id,
                amount_toman=charge,
                reason="reseller_trial",
                idempotency_key=f"reseller_trial:{order.id}",
                order_id=order.id,
            )
        except InsufficientWalletFunds as exc:
            raise TrialError("insufficient_funds") from exc

    try:
        account, _reward = await provision_order(
            session,
            client,
            order_id=order.id,
            default_group_ids=group_ids,
        )
    except Exception as exc:
        # Do not roll the order/debit back in a handler.  ``provision_order``
        # has already committed the durable create intent before panel I/O.
        raise TrialError(str(exc), order_id=int(order.id), original_exception=exc) from exc

    account.plan_fingerprint = trial_fingerprint(
        data_limit_gb=data_limit_gb, duration_days=duration_days
    )
    if display_name:
        # A shop with thirty services cannot tell them apart by panel username.
        account.display_name = display_name[:64]
    await session.flush()
    await session.refresh(order)
    return _trial_result(order=order, account=account, already_claimed=False, plan=plan)


async def resume_reseller_trial(
    session: AsyncSession,
    client: PasarGuardClient,
    *,
    user_id: int,
    order_id: int,
) -> TrialResult:
    """Resume one explicitly selected reseller giveaway, never a label match."""
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id, Order.user_id == user_id)
        .options(selectinload(Order.plan), selectinload(Order.user))
    )
    order = result.scalar_one_or_none()
    if (
        order is None
        or order.trial_kind != TRIAL_KIND_RESELLER
        or order.user is None
        or not order.user.is_reseller
    ):
        raise TrialError("reseller_trial_not_found")

    username = (order.panel_username or "").strip()
    if not username or not (order.customer_label or "").strip():
        raise TrialError("trial_recovery_unsafe", order_id=order.id)
    if order.status is OrderStatus.ACTIVE:
        account = await session.scalar(
            select(PasarGuardAccount).where(
                PasarGuardAccount.user_id == order.user_id,
                PasarGuardAccount.panel_username == username,
            )
        )
        if account is None:
            raise TrialError("trial_recovery_unsafe", order_id=order.id)
        return _trial_result(order=order, account=account, already_claimed=True, plan=order.plan)

    if order.status not in {OrderStatus.PROVISIONING, OrderStatus.PROVISION_FAILED}:
        raise TrialError("reseller_trial_not_found")
    if order.plan is None or not _has_complete_trial_snapshot(order):
        raise TrialError("trial_recovery_unsafe", order_id=order.id)

    from services.wallet import has_provision_fail_credit

    refunded = await has_provision_fail_credit(session, order.id)
    if int(order.price_toman) > 0 and not refunded:
        from services.finance import has_valid_reseller_trial_debit

        if not await has_valid_reseller_trial_debit(session, order):
            raise TrialError("reseller_trial_payment_unverified", order_id=order.id)

    debited_now = False
    if order.status is OrderStatus.PROVISION_FAILED:
        from services.wallet import (
            REASON_RETRY_DEBIT,
            InsufficientWalletFunds,
            debit,
            retry_debit_key,
        )

        if refunded:
            try:
                _entry, debited_now = await debit(
                    session,
                    user_id=order.user_id,
                    amount_toman=int(order.price_toman),
                    reason=REASON_RETRY_DEBIT,
                    idempotency_key=retry_debit_key(order.id),
                    order_id=order.id,
                    note="reseller_trial_retry_after_wallet_credit",
                )
            except InsufficientWalletFunds as exc:
                raise TrialError("insufficient_funds", order_id=order.id) from exc

    try:
        account, _reward = await provision_order(session, client, order_id=order.id)
    except Exception as exc:
        # Same as ``services.admin.retry_provision`` (F1 MONEY-2): the retry
        # debit is still uncommitted, and the handler commits after settling.
        # Keeping it charged the shop again for a giveaway that was never
        # delivered, and a terminal rejection could not refund it (the credit
        # key is one per order). Drop it; the order returns to «refunded,
        # retry costs nothing» and the next explicit retry charges once. If
        # provisioning committed its own intent first, the debit went with it
        # and the idempotent key keeps the next retry from charging twice.
        order_id = int(order.id)
        if debited_now:
            await session.rollback()
        raise TrialError(str(exc), order_id=order_id, original_exception=exc) from exc
    data_limit_gb, duration_days = _trial_snapshot(order)
    account.plan_fingerprint = trial_fingerprint(
        data_limit_gb=data_limit_gb,
        duration_days=duration_days,
    )
    await session.flush()
    await session.refresh(order)
    return _trial_result(order=order, account=account, already_claimed=False, plan=order.plan)
