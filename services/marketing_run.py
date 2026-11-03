"""The marketing jobs: the cart reminder (every 5 minutes), and the trial-ended and
winback nudges (hourly).

Every nudge goes through ``send_notice_once`` with a stable ``event_key``: claim,
commit, then send. A campaign that is off, or has nothing to send, writes nothing
(``tests/integration/test_idle_jobs_read_only.py``).

* **Retail only, and only events after switch-on.** ``services/marketing.py::may_nudge``
  answers who may be nudged; each campaign's candidate query starts at its own
  ``<name>_enabled_at``.
* **At most one capped nudge per user per 24 hours** (trial-ended and winback); the cart
  reminder is exempt. **At most 30 sends per run**, the rest wait for the next one.
* **The cart and trial-ended candidates are read from orders, never from a live account:**
  lifecycle may have soft-deleted a trial's account by then. Neither calls the panel.
* **Winback reads the panel once per candidate** and skips on a failed read or a service
  the panel does not call expired. It never sends on a failed read.
* **No read snapshot across a panel call or a send** (``end_read_transaction``).
* **A trial-ended or winback message may carry a personal offer** (``services/user_offers.py``):
  when the campaign's percent is above 0, ``send_notice_once`` writes the offer with the claim.
  The cart reminder never does.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import PasarGuardClient
from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.keyboards.marketing import cart_keyboard, trial_convert_keyboard, winback_keyboard
from bot.texts import texts
from database.base import end_read_transaction
from database.models import Order, OrderStatus, PasarGuardAccount, User
from database.models.lifecycle import ServiceNotice
from database.models.marketing import (
    CAMPAIGN_CART,
    CAMPAIGN_TRIAL_CONVERT,
    CAMPAIGN_WINBACK,
    NOTICE_MKT_CART,
    NOTICE_MKT_TRIAL_CONVERT,
    NOTICE_MKT_WINBACK,
)
from services.accounts import live_account_clause, service_list_label
from services.marketing import (
    CART_WINDOW_MINUTES,
    TRIAL_CONVERT_MAX_DAYS,
    TRIAL_CONVERT_MIN_HOURS,
    WINBACK_MAX_DAYS,
    WINBACK_MIN_DAYS,
    WINBACK_REPEAT_DAYS,
    may_nudge,
    nudged_within_cap,
)
from services.marketing_settings import MarketingConfig, load_config
from services.orders import user_has_successful_paid_order
from services.service_notices import send_notice_once
from services.user_offers import OfferSpec

logger = logging.getLogger(__name__)

MAX_SENDS_PER_RUN = 30
# Seconds between two panel reads: one user at a time, politely.
DEFAULT_PAUSE_SECONDS = 0.05
_CHUNK = 300

PANEL_EXPIRED = "expired"


# --- the cart reminder -------------------------------------------------------


@dataclass(frozen=True)
class _CartCandidate:
    order_id: int
    user_id: int
    telegram_id: int


async def _cart_candidates(
    session: AsyncSession, *, since: datetime, expiry_minutes: int, now: datetime
) -> list[_CartCandidate]:
    """Open orders in the last ``CART_WINDOW_MINUTES`` before they expire, made after switch-on."""
    youngest = now - timedelta(minutes=expiry_minutes - CART_WINDOW_MINUTES)
    oldest = now - timedelta(minutes=expiry_minutes)
    rows = (
        await session.execute(
            select(Order.id, Order.user_id, User.telegram_id)
            .join(User, User.id == Order.user_id)
            .where(
                Order.status == OrderStatus.PENDING_PAYMENT,
                Order.created_at >= since,
                Order.created_at <= youngest,
                Order.created_at > oldest,
            )
            .order_by(Order.id)
        )
    ).all()
    return [_CartCandidate(int(r[0]), int(r[1]), int(r[2])) for r in rows]


async def run_cart_nudges(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    """One pass. Returns how many reminders were sent. Never raises into the scheduler."""
    sent = 0
    try:
        moment = now or datetime.now(UTC)
        expiry = int(settings.order_expiry_minutes)
        async with session_factory() as session:
            since = (await load_config(session)).since(CAMPAIGN_CART)
            # Off, or an expiry too short to leave a window: nothing to read.
            if since is None or expiry <= CART_WINDOW_MINUTES:
                await end_read_transaction(session)
                return 0
            candidates = await _cart_candidates(
                session, since=since, expiry_minutes=expiry, now=moment
            )
            for item in candidates:
                if sent >= MAX_SENDS_PER_RUN:
                    break
                try:
                    if not await may_nudge(session, user_id=item.user_id, order_id=item.order_id):
                        continue
                    await end_read_transaction(session)
                    if await send_notice_once(
                        session_factory,
                        bot,
                        event_key=f"mkt:cart:{item.order_id}",
                        kind=NOTICE_MKT_CART,
                        account_id=None,
                        user_id=item.user_id,
                        chat_id=item.telegram_id,
                        text=texts.notify_cart_reminder,
                        markup=cart_keyboard(),
                        now=moment,
                    ):
                        sent += 1
                except Exception:
                    await session.rollback()
                    logger.exception("Cart reminder failed for order %s", item.order_id)
            await end_read_transaction(session)
    except Exception:
        logger.exception("marketing cart job failed")
    if sent:
        logger.info("Sent %s cart reminders", sent)
    return sent


# --- trial ended, never bought -----------------------------------------------


@dataclass(frozen=True)
class _TrialCandidate:
    order_id: int
    user_id: int
    telegram_id: int


async def _trial_candidates(
    session: AsyncSession, *, since: datetime, now: datetime
) -> list[_TrialCandidate]:
    """Personal trials that ended 24 hours to 7 days ago, after switch-on. From orders."""
    rows = (
        await session.execute(
            select(Order.id, Order.user_id, User.telegram_id)
            .join(User, User.id == Order.user_id)
            .where(
                Order.trial_kind.is_not(None),
                Order.trial_voided_at.is_(None),
                Order.status == OrderStatus.ACTIVE,
                Order.expires_at.is_not(None),
                Order.expires_at >= since,
                Order.expires_at <= now - timedelta(hours=TRIAL_CONVERT_MIN_HOURS),
                Order.expires_at > now - timedelta(days=TRIAL_CONVERT_MAX_DAYS),
            )
            .order_by(Order.expires_at, Order.id)
        )
    ).all()
    return [_TrialCandidate(int(r[0]), int(r[1]), int(r[2])) for r in rows]


async def _send_trial_convert(
    session: AsyncSession,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    item: _TrialCandidate,
    *,
    now: datetime,
    offer: OfferSpec | None = None,
) -> bool:
    if not await may_nudge(
        session,
        user_id=item.user_id,
        order_id=item.order_id,
        require_personal_trial=True,
        require_no_open_order=True,
    ):
        return False
    if await nudged_within_cap(session, user_id=item.user_id, now=now):
        return False
    # «Never bought» is the one definition the referral discount already uses.
    if await user_has_successful_paid_order(session, item.user_id):
        return False
    await end_read_transaction(session)
    return await send_notice_once(
        session_factory,
        bot,
        event_key=f"mkt:trial_convert:{item.user_id}",
        kind=NOTICE_MKT_TRIAL_CONVERT,
        account_id=None,
        user_id=item.user_id,
        chat_id=item.telegram_id,
        text=texts.notify_trial_convert,
        markup=trial_convert_keyboard(),
        now=now,
        offer=offer,
    )


# --- paid service ended, not renewed -----------------------------------------


@dataclass(frozen=True)
class _EndedService:
    account_id: int
    panel_username: str
    label: str
    ended_at: datetime
    user_id: int
    telegram_id: int


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _orders_by_account(
    session: AsyncSession, accounts: list[PasarGuardAccount]
) -> dict[int, list[Any]]:
    """The orders that reference each account (by panel username or as ``renew_account_id``)."""
    by_name = {str(a.panel_username): int(a.id) for a in accounts}
    ids = [int(a.id) for a in accounts]
    out: dict[int, list[Any]] = {i: [] for i in ids}
    seen: set[tuple[int, int]] = set()
    clauses = []
    names = list(by_name)
    for start in range(0, len(names), _CHUNK):
        clauses.append(Order.panel_username.in_(names[start : start + _CHUNK]))
    for start in range(0, len(ids), _CHUNK):
        clauses.append(Order.renew_account_id.in_(ids[start : start + _CHUNK]))
    for clause in clauses:
        rows = (
            await session.execute(
                select(
                    Order.id,
                    Order.status,
                    Order.trial_kind,
                    Order.expires_at,
                    Order.activated_at,
                    Order.created_at,
                    Order.panel_username,
                    Order.renew_account_id,
                ).where(or_(clause))
            )
        ).all()
        for row in rows:
            account_id = by_name.get(str(row.panel_username)) or row.renew_account_id
            if account_id is None or int(account_id) not in out:
                continue
            if (int(account_id), int(row.id)) in seen:
                continue
            seen.add((int(account_id), int(row.id)))
            out[int(account_id)].append(row)
    return out


def _latest(orders: list[Any]) -> Any | None:
    if not orders:
        return None
    return max(orders, key=lambda o: (_aware(o.activated_at or o.created_at), int(o.id)))


async def _ended_services(
    session: AsyncSession, *, since: datetime, now: datetime
) -> list[_EndedService]:
    """One ended paid service per user whose services have all ended.

    Resellers and banned users are loaded on purpose: ``may_nudge`` is the one place that
    decides who may be written to, and a second filter here would be a second definition.

    A user is a candidate only when **no** live service of theirs is still running. «Running»
    is read from the orders: the latest delivered order has no end or ends in the future; a
    service with no delivered order at all is unknown, and unknown counts as running.
    """
    rows = (
        await session.execute(
            select(PasarGuardAccount, User)
            .join(User, User.id == PasarGuardAccount.user_id)
            .where(live_account_clause())
            .order_by(PasarGuardAccount.id)
        )
    ).all()
    accounts = [account for account, _ in rows]
    orders = await _orders_by_account(session, accounts)
    earliest = now - timedelta(days=WINBACK_MAX_DAYS)
    latest_end = now - timedelta(days=WINBACK_MIN_DAYS)

    running_users: set[int] = set()
    ended: dict[int, list[_EndedService]] = defaultdict(list)
    for account, user in rows:
        delivered = [o for o in orders[int(account.id)] if o.status == OrderStatus.ACTIVE]
        newest = _latest(delivered)
        if newest is None or newest.expires_at is None or _aware(newest.expires_at) > now:
            running_users.add(int(user.id))
            continue
        paid = _latest([o for o in delivered if o.trial_kind is None])
        if paid is None or paid.expires_at is None:
            continue
        ends = _aware(paid.expires_at)
        if since <= ends <= latest_end and ends >= earliest:
            ended[int(user.id)].append(
                _EndedService(
                    account_id=int(account.id),
                    panel_username=str(account.panel_username),
                    label=service_list_label(account, reseller=False),
                    ended_at=ends,
                    user_id=int(user.id),
                    telegram_id=int(user.telegram_id),
                )
            )
    return [
        max(services, key=lambda s: (s.ended_at, s.account_id))
        for user_id, services in sorted(ended.items())
        if user_id not in running_users
    ]


async def _winbacked_recently(session: AsyncSession, user_id: int, now: datetime) -> bool:
    found = await session.scalar(
        select(ServiceNotice.id)
        .where(
            ServiceNotice.user_id == user_id,
            ServiceNotice.kind == NOTICE_MKT_WINBACK,
            ServiceNotice.sent_at.is_not(None),
            ServiceNotice.sent_at > now - timedelta(days=WINBACK_REPEAT_DAYS),
        )
        .limit(1)
    )
    return found is not None


async def _winback_key(session: AsyncSession, item: _EndedService, *, now: datetime) -> str | None:
    """The event key when this user may get a winback now, None when any rule says no."""
    if not await may_nudge(
        session, user_id=item.user_id, account_id=item.account_id, require_no_open_order=True
    ):
        return None
    if await nudged_within_cap(session, user_id=item.user_id, now=now):
        return None
    if await _winbacked_recently(session, item.user_id, now):
        return None
    key = f"mkt:winback:{item.user_id}:{item.ended_at.date().isoformat()}"
    already = await session.scalar(select(ServiceNotice.id).where(ServiceNotice.event_key == key))
    return None if already is not None else key


async def _send_winback(
    session: AsyncSession,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    panel: PasarGuardClient,
    item: _EndedService,
    *,
    now: datetime,
    offer: OfferSpec | None = None,
) -> bool | None:
    """True when sent, False when refused here, None when the panel was asked but said no."""
    key = await _winback_key(session, item, now=now)
    if key is None:
        return False
    # The panel has the last word on «ended». No read snapshot across it.
    await end_read_transaction(session)
    try:
        panel_user = await panel.get_user(item.panel_username)
    except Exception as exc:
        logger.warning(
            "Winback: panel read failed for %s (%s)", item.panel_username, type(exc).__name__
        )
        return None
    status = panel_user.status.value if panel_user.status is not None else ""
    if status != PANEL_EXPIRED:
        return None
    return await send_notice_once(
        session_factory,
        bot,
        event_key=key,
        kind=NOTICE_MKT_WINBACK,
        account_id=item.account_id,
        user_id=item.user_id,
        chat_id=item.telegram_id,
        text=texts.notify_winback.format(name=html.escape(item.label)),
        markup=winback_keyboard(),
        now=now,
        offer=offer,
    )


async def run_hourly_nudges(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    client: PasarGuardClient | None = None,
    now: datetime | None = None,
) -> int:
    """One pass over the trial-ended and winback campaigns. Never raises."""
    sent = 0
    try:
        moment = now or datetime.now(UTC)
        async with session_factory() as session:
            config = await load_config(session)
            if not (config.is_on(CAMPAIGN_TRIAL_CONVERT) or config.is_on(CAMPAIGN_WINBACK)):
                await end_read_transaction(session)
                return 0
            sent += await _trial_pass(
                session, session_factory, bot, config, now=moment, budget=MAX_SENDS_PER_RUN
            )
            sent += await _winback_pass(
                session,
                session_factory,
                bot,
                settings,
                config,
                now=moment,
                budget=MAX_SENDS_PER_RUN - sent,
                pause_seconds=pause_seconds,
                client=client,
            )
            await end_read_transaction(session)
    except Exception:
        logger.exception("marketing hourly job failed")
    if sent:
        logger.info("Sent %s marketing nudges", sent)
    return sent


async def _trial_pass(
    session: AsyncSession,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    config: MarketingConfig,
    *,
    now: datetime,
    budget: int,
) -> int:
    since = config.since(CAMPAIGN_TRIAL_CONVERT)
    if since is None or budget <= 0:
        return 0
    offer = config.offer(CAMPAIGN_TRIAL_CONVERT)
    sent = 0
    for item in await _trial_candidates(session, since=since, now=now):
        if sent >= budget:
            break
        try:
            if await _send_trial_convert(session, session_factory, bot, item, now=now, offer=offer):
                sent += 1
        except Exception:
            await session.rollback()
            logger.exception("Trial-ended nudge failed for order %s", item.order_id)
    await end_read_transaction(session)
    return sent


async def _winback_pass(
    session: AsyncSession,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    config: MarketingConfig,
    *,
    now: datetime,
    budget: int,
    pause_seconds: float,
    client: PasarGuardClient | None,
) -> int:
    since = config.since(CAMPAIGN_WINBACK)
    if since is None or budget <= 0:
        return 0
    offer = config.offer(CAMPAIGN_WINBACK)
    items = await _ended_services(session, since=since, now=now)
    await end_read_transaction(session)
    if not items:
        return 0
    own_client = client is None
    panel = client or await get_pasarguard_client(session, settings)
    await end_read_transaction(session)
    sent = 0
    try:
        asked = 0
        for item in items:
            if sent >= budget:
                break
            if asked and pause_seconds > 0:
                await asyncio.sleep(pause_seconds)
            try:
                outcome = await _send_winback(
                    session, session_factory, bot, panel, item, now=now, offer=offer
                )
            except Exception:
                await session.rollback()
                logger.exception("Winback nudge failed for user %s", item.user_id)
                continue
            if outcome is None:
                asked += 1
            elif outcome:
                asked += 1
                sent += 1
    finally:
        if own_client:
            await panel.aclose()
    return sent
