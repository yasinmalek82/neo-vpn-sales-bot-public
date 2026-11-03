"""What a person sees of their own account in the Mini App (read-only).

Two pages, one rule: **every query is keyed on the user row id the caller
passes, and the caller takes that id from the signed ``initData``, never from
the browser.** Nothing here writes, and nothing here returns a subscription
link (the item types have no field for one).

Panel reads are best-effort. A failed or slow read, or a panel confirmed down
(``panel_watch.is_panel_down``), drops the usage line of that service and
nothing else; the order's own expiry date is still shown. A successful read is
kept for ``LIVE_CACHE_SECONDS`` per person and service, in this process only,
so a page opened twice does not ask the panel twice. The bot's own list does
not cache, which is why the cache lives here and not in
``services.accounts.status_map_for_services``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.texts import texts
from database.models import PasarGuardAccount, WalletEntryType
from services.accounts import (
    RESELLER_PAGE_SIZE,
    LiveStatus,
    ServicePage,
    account_label,
    list_named_services,
    list_services_page,
    service_expire_hint,
    service_list_label,
    status_map_for_services,
)
from services.panel_watch import is_panel_down
from services.reporting import FinanceWindow, user_sales
from services.trial import ResellerTrialQuota, get_reseller_trial_quota
from services.ux_format import (
    format_toman,
    is_expiring_soon,
    service_status_emoji,
    to_persian_digits,
)
from services.wallet import get_balance, list_recent_entries, wallet_reason_label

logger = logging.getLogger(__name__)

LIVE_CACHE_SECONDS = 60.0
LIVE_CACHE_MAX_ROWS = 512
# One panel read and the whole page's worth. The client has its own, longer
# timeout; a person waiting on a phone should not wait for it ten times over.
PANEL_READ_TIMEOUT_SECONDS = 4.0
PANEL_READ_BUDGET_SECONDS = 8.0
WALLET_ENTRIES_SHOWN = 8

_live_cache: dict[tuple[int, int], tuple[float, LiveStatus]] = {}


def clear_live_cache() -> None:
    """For tests; the cache holds nothing a restart needs."""
    _live_cache.clear()


@dataclass(frozen=True)
class ServiceItem:
    """One service as a page shows it. No link, no token, no panel username."""

    account_id: int
    name: str
    live: LiveStatus
    emoji: str
    expiring_soon: bool


@dataclass(frozen=True)
class WalletEntryView:
    label_fa: str
    amount_toman: int
    is_credit: bool
    created_at: datetime


@dataclass(frozen=True)
class WalletView:
    balance_toman: int
    entries: list[WalletEntryView]


@dataclass(frozen=True)
class CustomerPage:
    services: list[ServiceItem]
    wallet: WalletView


@dataclass(frozen=True)
class ResellerDashboard:
    today: FinanceWindow
    month: FinanceWindow
    total: FinanceWindow
    trial: ResellerTrialQuota
    trial_note: str
    page: ServicePage
    services: list[ServiceItem]
    wallet: WalletView


async def wallet_view(session: AsyncSession, user_id: int) -> WalletView:
    """Balance and recent entries. The entry's ``note`` (an admin's words) is left out."""
    balance = await get_balance(session, user_id)
    rows = await list_recent_entries(session, user_id=user_id, limit=WALLET_ENTRIES_SHOWN)
    return WalletView(
        balance_toman=balance,
        entries=[
            WalletEntryView(
                label_fa=wallet_reason_label(row.reason),
                amount_toman=int(row.amount_toman),
                is_credit=row.entry_type == WalletEntryType.credit,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


def _cached(user_id: int, account_id: int, now: float) -> LiveStatus | None:
    hit = _live_cache.get((user_id, account_id))
    if hit is None or now - hit[0] >= LIVE_CACHE_SECONDS:
        return None
    return hit[1]


def _remember(user_id: int, account_id: int, item: LiveStatus, now: float) -> None:
    if len(_live_cache) >= LIVE_CACHE_MAX_ROWS:
        for key, (stamp, _) in list(_live_cache.items()):
            if now - stamp >= LIVE_CACHE_SECONDS:
                del _live_cache[key]
        if len(_live_cache) >= LIVE_CACHE_MAX_ROWS:
            _live_cache.clear()
    _live_cache[(user_id, account_id)] = (now, item)


async def _read_panel(
    session: AsyncSession, settings: Settings, missing: list[PasarGuardAccount]
) -> dict[int, LiveStatus]:
    """Ask the panel about ``missing``; an empty dict on any failure, never an exception."""
    try:
        if await is_panel_down(session, settings):
            return {}
        client = await get_pasarguard_client(session, settings)
    except Exception as exc:
        logger.warning("personal page: panel not reachable (%s)", type(exc).__name__)
        return {}
    try:
        return await status_map_for_services(
            session,
            client,
            missing,
            read_timeout=PANEL_READ_TIMEOUT_SECONDS,
            budget=PANEL_READ_BUDGET_SECONDS,
        )
    except Exception as exc:
        logger.warning("personal page: panel read failed (%s)", type(exc).__name__)
        return {}
    finally:
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("personal page: client close failed (%s)", type(exc).__name__)


async def live_for_services(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    accounts: list[PasarGuardAccount],
) -> dict[int, LiveStatus]:
    """Live status per account id: cache, then the panel, then the order's expiry date."""
    result: dict[int, LiveStatus] = {}
    missing: list[PasarGuardAccount] = []
    now = time.monotonic()
    for account in accounts:
        hit = _cached(user_id, account.id, now)
        if hit is not None:
            result[account.id] = hit
        else:
            missing.append(account)
    if not missing:
        return result
    fresh = await _read_panel(session, settings, missing)
    for account in missing:
        item = fresh.get(account.id)
        if item is None:
            item = LiveStatus(status=None, expire=await service_expire_hint(session, account))
        elif item.from_panel:
            _remember(user_id, account.id, item, now)
        result[account.id] = item
    return result


def _item(
    account: PasarGuardAccount, name: str, live: LiveStatus, *, warn_days: int
) -> ServiceItem:
    return ServiceItem(
        account_id=account.id,
        name=name,
        live=live,
        emoji=service_status_emoji(live.status, live.expire, warn_days=warn_days),
        expiring_soon=is_expiring_soon(live.expire, warn_days=warn_days),
    )


async def customer_page(session: AsyncSession, settings: Settings, user_id: int) -> CustomerPage:
    """The customer's services (the list «سرویس‌های من» shows) and wallet."""
    accounts = await list_named_services(session, user_id)
    wallet = await wallet_view(session, user_id)
    live = await live_for_services(session, settings, user_id, accounts)
    warn = int(settings.service_expire_warn_days)
    return CustomerPage(
        services=[_item(a, account_label(a), live[a.id], warn_days=warn) for a in accounts],
        wallet=wallet,
    )


def trial_note(trial: ResellerTrialQuota) -> str:
    """What the quota means right now, in the bot's own words (free left, paid, spent)."""
    if not trial.can_issue:
        return texts.reseller_trial_exhausted.format(
            quota=to_persian_digits(trial.quota), period=to_persian_digits(trial.period_label)
        )
    if trial.is_free:
        return texts.reseller_trial_label_free.format(remaining=to_persian_digits(trial.remaining))
    return texts.reseller_trial_label_paid.format(
        price=format_toman(trial.price_toman, persian_digits=True)
    )


async def reseller_dashboard(
    session: AsyncSession, settings: Settings, user_id: int, *, offset: int = 0
) -> ResellerDashboard:
    """A shop's sales, one page of its customers' services, trial quota and wallet."""
    today, month, total = await user_sales(session, user_id)
    trial = await get_reseller_trial_quota(session, user_id)
    page = await list_services_page(session, user_id, offset=offset, limit=RESELLER_PAGE_SIZE)
    wallet = await wallet_view(session, user_id)
    live = await live_for_services(session, settings, user_id, page.rows)
    warn = int(settings.service_expire_warn_days)
    return ResellerDashboard(
        today=today,
        month=month,
        total=total,
        trial=trial,
        trial_note=trial_note(trial),
        page=page,
        services=[
            _item(a, service_list_label(a, reseller=True), live[a.id], warn_days=warn)
            for a in page.rows
        ],
        wallet=wallet,
    )
