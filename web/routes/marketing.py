"""«📣 پیام‌های بازاریابی»: the four campaigns, their switch and their offer, settings only.

The page is the bot's screen: ``services/marketing_view.campaign_cards`` composes each
campaign's title, state, description and offer line for both. A write is a ``POST`` into
the bot's own service functions (``set_campaign``, ``set_offer_percent``,
``set_offer_hours``) through ``services/marketing_edit``: a store owner only, and only
against the token the form was drawn from, so a stale or double-submitted form is refused.

This page sends nothing, previews no customer message and has no «run now»: a campaign
acts on its own schedule, only on events after it was switched on, only for retail
customers (``.claude/rules/marketing.md``). A GET never writes, and a percent stays
editable while its campaign is off.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from bot.texts import texts
from database.models.marketing import CAMPAIGNS, MAX_OFFER_HOURS, MAX_OFFER_PERCENT
from services.marketing_edit import (
    MarketingOwnerRequired,
    StaleMarketingSettings,
    edit_campaign_switch,
    edit_offer_hours,
    edit_offer_percent,
    read_marketing,
)
from services.marketing_settings import MarketingSettingsError
from services.marketing_view import campaign_cards
from services.store_config import is_store_owner
from services.ux_format import to_persian_digits
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.templating import render

MARKETING_URL = f"{PANEL_PREFIX}/marketing"
SAVED = texts.admin_mkt_saved


async def _page(
    request: web.Request,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    notice_campaign: str | None = None,
    status: int = 200,
) -> web.Response:
    owner = is_store_owner(request.app[SETTINGS], request["telegram_id"])
    async with request.app[SESSION_FACTORY]() as session:
        config, token = await read_marketing(session)
    head, _, intro = texts.admin_mkt_screen.format(lines="").strip().partition("\n\n")
    return render(
        request,
        "marketing.html",
        status=status,
        head=head,
        intro=intro,
        cards=campaign_cards(config),
        can_edit=owner,
        read_only_hint=None if owner else texts.admin_mkt_not_owner,
        token=token,
        notice=notice,
        notice_is_error=notice_is_error,
        notice_campaign=notice_campaign,
        marketing_url=MARKETING_URL,
        percent_hint=texts.admin_mkt_offer_percent_range.format(
            high=to_persian_digits(MAX_OFFER_PERCENT)
        ),
        hours_hint=texts.admin_mkt_offer_hours_range.format(
            high=to_persian_digits(MAX_OFFER_HOURS)
        ),
    )


def _refused(request: web.Request, status: int, message: str) -> web.Response:
    return render(
        request,
        "store_refused.html",
        status=status,
        message=message,
        reload_url=MARKETING_URL,
    )


async def _refusal_page(
    request: web.Request, refusal: MarketingSettingsError, campaign: str
) -> web.Response:
    """Owner 403, stale 409 (a reload button), anything else 400 under its campaign."""
    if isinstance(refusal, MarketingOwnerRequired):
        return _refused(request, 403, str(refusal))
    if isinstance(refusal, StaleMarketingSettings):
        return _refused(request, 409, str(refusal))
    return await _page(
        request,
        notice=texts.admin_mkt_offer_input_error.format(reason=refusal),
        notice_is_error=True,
        notice_campaign=campaign if campaign in CAMPAIGNS else None,
        status=400,
    )


def _seen(form: Any) -> str | None:
    value = str(form.get("seen", ""))
    return value or None


async def _write(request: web.Request, write: Any) -> web.Response:
    campaign = request.match_info["campaign"]
    form = await request.post()
    refusal: MarketingSettingsError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            await write(session, request.app[SETTINGS], request["telegram_id"], form, campaign)
        except MarketingSettingsError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, campaign)
    return await _page(request, notice=SAVED, notice_campaign=campaign)


async def marketing_home(request: web.Request) -> web.Response:
    return await _page(request)


async def marketing_toggle(request: web.Request) -> web.Response:
    async def write(session: Any, settings: Any, tg_id: int, form: Any, campaign: str) -> None:
        await edit_campaign_switch(
            session,
            settings,
            tg_id,
            seen=_seen(form),
            campaign=campaign,
            to=str(form.get("to", "")),
        )

    return await _write(request, write)


async def marketing_percent(request: web.Request) -> web.Response:
    async def write(session: Any, settings: Any, tg_id: int, form: Any, campaign: str) -> None:
        await edit_offer_percent(
            session,
            settings,
            tg_id,
            seen=_seen(form),
            campaign=campaign,
            raw=str(form.get("percent", "")),
        )

    return await _write(request, write)


async def marketing_hours(request: web.Request) -> web.Response:
    async def write(session: Any, settings: Any, tg_id: int, form: Any, campaign: str) -> None:
        await edit_offer_hours(
            session,
            settings,
            tg_id,
            seen=_seen(form),
            campaign=campaign,
            raw=str(form.get("hours", "")),
        )

    return await _write(request, write)


routes = [
    web.get(MARKETING_URL, marketing_home),
    web.post(f"{MARKETING_URL}/{{campaign}}/toggle", marketing_toggle),
    web.post(f"{MARKETING_URL}/{{campaign}}/percent", marketing_percent),
    web.post(f"{MARKETING_URL}/{{campaign}}/hours", marketing_hours),
]
