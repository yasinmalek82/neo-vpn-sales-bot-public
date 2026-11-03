"""«حجم داره تموم می‌شه»: one warning per threshold per period.

``volume_verdict`` is a pure function of the configured percents and what the
panel says now. ``warn_low_volume`` (the job body) reads the panel one user
at a time, claims the warning (``services/service_notices.py``) and sends it.

* **The lowest crossed percent wins**: with «۲۰ ۵», a service at 15% left gets
  the 20 warning, and one that jumps straight to 3% gets only the 5 warning.
* **The period is (data limit, expiry).** Added volume or a renewal changes it,
  so both warnings are armed again. A usage reset that changes neither (a
  monthly strategy) re-arms when the service is seen above every percent.
* **A free trial never warns** (it cannot be renewed; `services/account_orders.py`).
* **Unlimited (limit 0 or None) never warns.** Only an ``active`` service is
  warned: a limited or expired one is already cut off.
* **No read snapshot across the panel call or the send**
  (``end_read_transaction``). A panel error for one service skips it.
* **The upgrade wording may carry a personal offer** (``services/user_offers.py``): the
  wording is decided before the claim, and the offer row is written in the claim's own
  transaction. Its ``min_volume_gb`` is the service's current limit in whole GB.
  The wording always goes out, the offer does not when the customer has an open order
  (they would be taken back to it at its old price) or when this service already has a
  not-voided upgrade offer in this period (one per service per period, whatever the
  threshold). A failed send voids the offer with the released claim.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import PasarGuardClient
from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.keyboards.account import service_detail_keyboard
from bot.keyboards.marketing import volume_upgrade_keyboard
from bot.texts import texts
from database.base import end_read_transaction
from database.models import PasarGuardAccount, User
from database.models.lifecycle import NOTICE_VOLUME_WARNING
from database.models.marketing import CAMPAIGN_UPGRADE
from services.account_orders import admin_touched_ids, is_trial_by_refs, load_order_refs
from services.accounts import live_account_clause, normalize_expire, service_list_label
from services.lifecycle_settings import load_config
from services.marketing_settings import load_config as load_marketing_config
from services.orders import user_has_open_order
from services.service_notices import (
    claim_notice,
    forget_notices,
    is_claimed,
    is_permanent_send_failure,
    mark_delivered,
    release_notice,
)
from services.user_offers import (
    OfferSpec,
    offer_is_spent,
    offer_line,
    unvoided_offer_exists,
    void_offer,
)
from services.ux_format import to_persian_digits
from services.volume_upgrade import UpgradeOffer, upgrade_offer

logger = logging.getLogger(__name__)

NO_WARNING = "none"
HEALTHY = "healthy"
WARN = "warn"

# Seconds between two panel reads: one user at a time, politely.
DEFAULT_PAUSE_SECONDS = 0.05

_BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class VolumeVerdict:
    kind: str
    threshold: int | None = None
    left_percent: float | None = None


def volume_verdict(
    percents: Sequence[int],
    *,
    data_limit: int | None,
    used_traffic: int,
    status: str,
) -> VolumeVerdict:
    """What the panel's numbers mean for the configured percents."""
    if not percents or not data_limit or data_limit <= 0 or status != "active":
        return VolumeVerdict(NO_WARNING)
    left = max(0, int(data_limit) - int(used_traffic)) / int(data_limit) * 100
    crossed = [int(p) for p in percents if left <= int(p)]
    if crossed:
        return VolumeVerdict(WARN, min(crossed), left)
    return VolumeVerdict(HEALTHY, None, left)


def period_of(data_limit: int, expire: Any) -> str:
    """Changes when the limit or the expiry does. ``expire`` 0 or None is «never»."""
    moment = normalize_expire(expire) if expire not in (None, 0) else None
    return f"{int(data_limit)}:{int(moment.timestamp()) if moment else 0}"


def event_key(account_id: int, threshold: int, period: str) -> str:
    return f"volwarn:{account_id}:{threshold}:{period}"


def period_key_pattern(account_id: int, period: str) -> str:
    """The SQL LIKE pattern of ``event_key`` for every threshold of one service period.

    The account id and the period hold only digits and colons, so nothing in them is a
    LIKE wildcard (``%`` or ``_``) and no escaping is needed.
    """
    return f"volwarn:{account_id}:%:{period}"


@dataclass(frozen=True)
class _Target:
    account_id: int
    panel_username: str
    owner_id: int
    owner_telegram_id: int
    label: str


async def _targets(session: AsyncSession) -> list[_Target]:
    """Live services of customers who can be written to. Plain data, no ORM row."""
    rows = (
        await session.execute(
            select(PasarGuardAccount, User)
            .join(User, User.id == PasarGuardAccount.user_id)
            .where(live_account_clause(), User.is_banned.is_(False))
            .order_by(PasarGuardAccount.id)
        )
    ).all()
    # A free trial cannot be renewed, so «۲۰٪ مانده» with a renew button would lead
    # nowhere: trials get no volume warning.
    refs = await load_order_refs(
        session, [(int(account.id), str(account.panel_username)) for account, _ in rows]
    )
    touched = await admin_touched_ids(session, [int(account.id) for account, _ in rows])
    return [
        _Target(
            account_id=int(account.id),
            panel_username=str(account.panel_username),
            owner_id=int(user.id),
            owner_telegram_id=int(user.telegram_id),
            label=service_list_label(account, reseller=bool(user.is_reseller)),
        )
        for account, user in rows
        if not is_trial_by_refs(
            refs.get(int(account.id), []), admin_touched=int(account.id) in touched
        )
    ]


async def warn_low_volume(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    client: PasarGuardClient | None = None,
    now: datetime | None = None,
) -> int:
    """One pass. Returns how many warnings were sent. Never raises into the scheduler."""
    sent = 0
    try:
        async with session_factory() as session:
            config = await load_config(session, seed_days=settings.renewal_reminder_days)
            if not config.warn_volume_percents:
                return 0
            targets = await _targets(session)
            marketing = await load_marketing_config(session)
            upgrade_since = marketing.since(CAMPAIGN_UPGRADE)
            upgrade_offer = marketing.offer(CAMPAIGN_UPGRADE)
            await end_read_transaction(session)
            if not targets:
                return 0
            own_client = client is None
            panel = client or await get_pasarguard_client(session, settings)
            await end_read_transaction(session)
            try:
                for index, target in enumerate(targets):
                    if index and pause_seconds > 0:
                        await asyncio.sleep(pause_seconds)
                    try:
                        if await _one(
                            session,
                            bot,
                            panel,
                            target,
                            percents=config.warn_volume_percents,
                            now=now or datetime.now(UTC),
                            upgrade_since=upgrade_since,
                            upgrade_offer=upgrade_offer,
                        ):
                            sent += 1
                    except Exception:
                        await session.rollback()
                        logger.exception("Volume warning failed for %s", target.panel_username)
            finally:
                if own_client:
                    await panel.aclose()
    except Exception:
        logger.exception("volume_warnings job failed")
    if sent:
        logger.info("Sent %s volume warnings", sent)
    return sent


async def _one(
    session: AsyncSession,
    bot: Bot,
    client: PasarGuardClient,
    target: _Target,
    *,
    percents: Sequence[int],
    now: datetime,
    upgrade_since: datetime | None = None,
    upgrade_offer: OfferSpec | None = None,
) -> bool:
    # No read snapshot across the panel call or the send: a job committing meanwhile
    # would make the claim below fail with «database is locked».
    await end_read_transaction(session)
    try:
        panel_user = await client.get_user(target.panel_username)
    except Exception as exc:
        # A timeout, a 5xx or a missing user: skip this one, go on.
        logger.warning(
            "Volume warning: panel read failed for %s (%s)",
            target.panel_username,
            type(exc).__name__,
        )
        return False
    status = panel_user.status.value if panel_user.status is not None else ""
    verdict = volume_verdict(
        percents,
        data_limit=panel_user.data_limit,
        used_traffic=int(panel_user.used_traffic or 0),
        status=status,
    )
    if verdict.kind == HEALTHY:
        if await forget_notices(session, account_id=target.account_id, kind=NOTICE_VOLUME_WARNING):
            await session.commit()
        else:
            await end_read_transaction(session)
        return False
    if verdict.kind != WARN or verdict.threshold is None:
        return False

    period = period_of(int(panel_user.data_limit or 0), panel_user.expire)
    key = event_key(target.account_id, verdict.threshold, period)
    wording: UpgradeOffer | None = None
    offer: OfferSpec | None = None
    # An already-claimed warning is not looked at again: nothing to decide, nothing to write,
    # so an idle run stays as cheap as before.
    if (
        upgrade_since is not None
        and now >= upgrade_since
        and not await is_claimed(session, event_key=key, now=now)
    ):
        wording = await _upgrade_wording(
            session,
            target,
            data_limit=int(panel_user.data_limit or 0),
            expire=panel_user.expire,
            now=now,
        )
        if (
            wording is not None
            and upgrade_offer is not None
            and await _offer_may_be_promised(
                session, target, campaign=upgrade_offer.campaign, key=key, period=period
            )
        ):
            offer = replace(
                upgrade_offer, min_volume_gb=int(panel_user.data_limit or 0) // _BYTES_PER_GB
            )
    claimed = await claim_notice(
        session,
        event_key=key,
        kind=NOTICE_VOLUME_WARNING,
        account_id=target.account_id,
        user_id=target.owner_id,
        now=now,
        offer=offer,
    )
    if not claimed:
        # Nothing was written (see claim_notice), so no commit: an idle run is read-only.
        await end_read_transaction(session)
        return False
    # The claim is durable before the send: a crash loses this warning, it
    # never sends it twice.
    await session.commit()
    percent = max(0, min(100, int(round(verdict.left_percent or 0))))
    text = texts.notify_volume_warning.format(
        name=html.escape(target.label), percent=to_persian_digits(percent)
    )
    markup = service_detail_keyboard(target.account_id, has_link=False, show_renew=True)
    if wording is not None:
        text = texts.notify_volume_upgrade.format(
            name=html.escape(target.label),
            percent=to_persian_digits(percent),
            days=to_persian_digits(wording.days_left),
            package=html.escape(wording.package_title),
        )
        if offer is not None:
            text = f"{text}\n\n{offer_line(offer)}"
        markup = volume_upgrade_keyboard(target.account_id)
    # The decision above read the database: no snapshot across the send.
    await end_read_transaction(session)
    try:
        await bot.send_message(chat_id=target.owner_telegram_id, text=text, reply_markup=markup)
    except Exception as exc:
        logger.warning(
            "Volume warning send failed for %s (%s)", target.panel_username, type(exc).__name__
        )
        if is_permanent_send_failure(exc):
            await mark_delivered(session, event_key=key, refused_for_good=True)
            if offer is not None:
                await void_offer(session, event_key=key)
        else:
            await release_notice(session, event_key=key)
            if offer is not None:
                await void_offer(session, event_key=key)
        await session.commit()
        return False
    await mark_delivered(session, event_key=key)
    await session.commit()
    return True


async def _offer_may_be_promised(
    session: AsyncSession, target: _Target, *, campaign: str, key: str, period: str
) -> bool:
    """May the upgrade wording of this warning carry an offer? Reads only.

    No when the customer has an open order (an offer would be promised to someone who
    cannot use it), and no when the service already has a not-voided upgrade offer in
    this period under another threshold. The key being claimed is left out of that
    check, so a retry of the same warning finds its own row and still carries the line;
    but not when an order already took that row (the same key comes back after a
    periodic reset): a spent offer is not promised again.
    """
    if await user_has_open_order(session, target.owner_id):
        return False
    if await offer_is_spent(session, event_key=key):
        return False
    return not await unvoided_offer_exists(
        session,
        user_id=target.owner_id,
        campaign=campaign,
        event_key_like=period_key_pattern(target.account_id, period),
        except_event_key=key,
    )


async def _upgrade_wording(
    session: AsyncSession,
    target: _Target,
    *,
    data_limit: int,
    expire: Any,
    now: datetime,
) -> UpgradeOffer | None:
    """The upgrade wording for this warning, or None for the standard one. Never raises:
    a failure here must cost the customer nothing but the nicer wording."""
    try:
        return await upgrade_offer(
            session,
            account_id=target.account_id,
            user_id=target.owner_id,
            data_limit_bytes=data_limit,
            expire=expire,
            now=now,
        )
    except Exception:
        logger.exception("Upgrade wording failed for %s", target.panel_username)
        return None
