"""Marketing settings written from the panel: owner only, against what the form was drawn from.

The campaigns' switches and offers are not in the store version, so the store's
version check cannot guard them. A marketing form carries a **token** (the settings
row's ``updated_at``, or ``none`` while no row exists), and a write is refused when the
token read *inside the transaction that writes* is not the one the page was drawn from.
So a form drawn before another edit (the bot's, another tab's) is refused and never
merged, and a double-submitted toggle applies once: the first write moves the token, the
second form still carries the old one and cannot flip the campaign back.

The writes themselves are the bot's own (``set_campaign``, ``set_offer_percent``,
``set_offer_hours`` with ``parse_offer_percent`` / ``parse_offer_hours``): same parsing,
same refusal lines. Reading never writes (no row means every campaign is off).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.marketing import MARKETING_SETTINGS_ROW_ID, MarketingSettings
from services.marketing_settings import (
    MarketingConfig,
    MarketingSettingsError,
    config_from_row,
    parse_offer_hours,
    parse_offer_percent,
    set_campaign,
    set_offer_hours,
    set_offer_percent,
)
from services.store_config import is_store_owner

_T = TypeVar("_T")

NO_ROW_TOKEN = "none"

TEXT_STALE_MARKETING = (
    "تنظیمات پیام‌های بازاریابی در این فاصله عوض شد. چیزی ذخیره نشد؛ صفحه را دوباره باز کن."
)


class MarketingOwnerRequired(MarketingSettingsError):
    """The caller is an admin but not a store owner."""


class StaleMarketingSettings(MarketingSettingsError):
    """The settings changed since the form was drawn; nothing was written."""


def settings_token(row: MarketingSettings | None) -> str:
    """What a form is drawn from: the row's last change, or ``none`` while there is no row."""
    if row is None:
        return NO_ROW_TOKEN
    return row.updated_at.isoformat()


async def read_marketing(session: AsyncSession) -> tuple[MarketingConfig, str]:
    """The switches and the token of the *same* read. Never writes."""
    row = await session.get(MarketingSettings, MARKETING_SETTINGS_ROW_ID)
    return config_from_row(row), settings_token(row)


async def guarded_marketing_write(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    seen: str | None,
    action: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    """Every panel write to the marketing settings: owner only, the form's token only.

    ``action`` runs in the transaction that read the token; the commit is here. The one
    place that maps SQLite's «database is locked» (and a lost race to create the row) to
    the stale refusal, so a setting added later cannot forget it.
    """
    if not is_store_owner(settings, telegram_id):
        raise MarketingOwnerRequired(texts.admin_mkt_not_owner)
    try:
        row = await session.get(MarketingSettings, MARKETING_SETTINGS_ROW_ID, with_for_update=True)
        if seen is None or settings_token(row) != seen:
            raise StaleMarketingSettings(TEXT_STALE_MARKETING)
        result = await action(session)
        await session.commit()
        return result
    except MarketingSettingsError:
        await session.rollback()
        raise
    except (OperationalError, IntegrityError) as exc:
        # SQLite refuses a writer whose snapshot another commit has passed; two first
        # writers both insert row 1. Either is this check losing a race: nothing was written.
        await session.rollback()
        if isinstance(exc, IntegrityError) or "locked" in str(exc).lower():
            raise StaleMarketingSettings(TEXT_STALE_MARKETING) from exc
        raise


async def edit_campaign_switch(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    seen: str | None,
    campaign: str,
    to: str,
) -> None:
    """Switch a campaign ``on`` or ``off``. Anything else switches nothing: no default."""

    async def action(s: AsyncSession) -> None:
        if to not in ("on", "off"):
            raise MarketingSettingsError(texts.admin_action_error)
        await set_campaign(s, campaign=campaign, on=to == "on")

    await guarded_marketing_write(session, settings, telegram_id, seen=seen, action=action)


async def edit_offer_percent(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    seen: str | None,
    campaign: str,
    raw: str,
) -> None:
    """The percent is editable whether the campaign is on or off: it is read when it sends."""

    async def action(s: AsyncSession) -> None:
        await set_offer_percent(s, campaign=campaign, percent=parse_offer_percent(raw))

    await guarded_marketing_write(session, settings, telegram_id, seen=seen, action=action)


async def edit_offer_hours(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    seen: str | None,
    campaign: str,
    raw: str,
) -> None:
    async def action(s: AsyncSession) -> None:
        await set_offer_hours(s, campaign=campaign, hours=parse_offer_hours(raw))

    await guarded_marketing_write(session, settings, telegram_id, seen=seen, action=action)
