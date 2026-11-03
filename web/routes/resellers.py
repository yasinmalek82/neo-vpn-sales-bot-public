"""«🏪 نمایندگان»: the resellers and the price sheet of each, with the bot's own writes.

The bot's reseller controls, on the web (``.claude/rules/web-panel.md``): a plan's price and
its clear, the rate (toman per GB, toman per day), the monthly trial quota and the price of
an extra trial, and the percent cut over one tab with its undo. Every write is a ``POST``
into a function of ``services/reseller_edit.py`` that makes the bot's call with the bot's
parsing and refusal lines; the form carries what it was drawn from and a stale or
double-submitted form is refused (409), never merged. Any admin may use them, as in the bot.

Granting or removing the reseller role stays in the bot: no route here touches it, and a
person who is not a reseller has no sheet (404). Nothing on these pages changes a retail
price, a promo, an offer or a wallet. A GET never writes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from services.reseller import ResellerError
from services.reseller_admin import (
    TEXT_STALE_RESELLER,
    NotAReseller,
    StaleResellerSheet,
    decode_duration,
)
from services.reseller_edit import (
    NothingToUndo,
    apply_planned_cut,
    clear_cell_price,
    edit_plan_price,
    edit_rate_field,
    plan_cut,
    undo_cut,
)
from services.reseller_sheet import TEXT_CROSSED, build_sheet, list_resellers
from services.store_pricing import DEFAULT_TIER
from web.keys import NUMERIC_ID_PATTERN, PANEL_PREFIX, SESSION_FACTORY
from web.templating import render

RESELLERS_URL = f"{PANEL_PREFIX}/resellers"
MAX_FIELD = 128  # a slug, a tier or a category slug never needs more


def _body(intro: str) -> str:
    """A bot intro without its title line (the section heading already says it)."""
    return intro.partition("\n\n")[2] or intro


# What the bot's reseller screens say, handed to the templates (they decide nothing).
_WORDS = {
    "crossed": TEXT_CROSSED,
    "custom_intro": _body(texts.admin_reseller_custom_intro),
    "trial_intro": _body(texts.admin_reseller_trial_intro),
    "flat_hint": texts.admin_reseller_flat_hint,
    "no_plans": texts.admin_reseller_no_plans,
    "no_plans_tab": texts.admin_reseller_no_plans_tab,
    "pct_prompt": texts.web_reseller_pct_prompt,
    "pct_prompt_flat": texts.web_reseller_pct_prompt_flat,
    "undo_btn": texts.admin_reseller_undo_btn,
    "cell_none": texts.web_reseller_cell_none,
    "month_hint": texts.admin_resellers_month_hint,
}

Write = Callable[[AsyncSession, int, Any], Awaitable[str]]


def _uid(request: web.Request) -> int:
    return int(request.match_info["user_id"])


def _not_found(request: web.Request) -> web.Response:
    return render(request, "message.html", status=404, message=texts.admin_customer_not_found)


def _field(source: Any, name: str) -> str:
    return str(source.get(name, ""))[:MAX_FIELD]


def _seen(form: Any) -> str | None:
    return _field(form, "seen") or None


def _duration(raw: str) -> int | None:
    """A duration the form carried; one that is not a duration is a stale form."""
    try:
        return decode_duration(raw)
    except ValueError as exc:
        raise StaleResellerSheet(TEXT_STALE_RESELLER) from exc


def _reload_url(uid: int, form: Any) -> str:
    """The sheet on the tab the form came from."""
    base = f"{RESELLERS_URL}/{uid}"
    category = _field(form, "c")
    if not category:
        return base
    parts = (("c", category), ("d", _field(form, "d")), ("t", _field(form, "t")))
    return base + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in parts)


async def _sheet_page(
    request: web.Request,
    uid: int,
    tab: Any,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    notice_in: str | None = None,
    status: int = 200,
) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        view = await build_sheet(
            session,
            uid,
            category_slug=_field(tab, "c"),
            duration=_field(tab, "d"),
            tier=_field(tab, "t"),
        )
    if view is None:
        return _not_found(request)
    return render(
        request,
        "reseller_sheet.html",
        status=status,
        sheet=view,
        words=_WORDS,
        sheet_url=f"{RESELLERS_URL}/{uid}",
        list_url=RESELLERS_URL,
        customer_url=f"{PANEL_PREFIX}/customers/{uid}",
        notice=notice,
        notice_is_error=notice_is_error,
        notice_in=notice_in,
    )


def _refused(request: web.Request, status: int, message: str, uid: int, form: Any) -> web.Response:
    return render(
        request,
        "store_refused.html",
        status=status,
        message=message,
        reload_url=_reload_url(uid, form),
    )


async def _refusal_page(
    request: web.Request, refusal: ResellerError, uid: int, form: Any, where: str
) -> web.Response:
    """Not a reseller 404; a stale form or an undo with nothing to take back 409;
    anything else is the bot's own line, shown under its section (400)."""
    if isinstance(refusal, NotAReseller):
        return _not_found(request)
    if isinstance(refusal, StaleResellerSheet | NothingToUndo):
        return _refused(request, 409, str(refusal), uid, form)
    return await _sheet_page(
        request,
        uid,
        form,
        notice=str(refusal),
        notice_is_error=True,
        notice_in=where,
        status=400,
    )


async def _write(request: web.Request, where: str, write: Write) -> web.Response:
    uid = _uid(request)
    form = await request.post()
    refusal: ResellerError | None = None
    saved = ""
    async with request.app[SESSION_FACTORY]() as session:
        try:
            saved = await write(session, uid, form)
        except ResellerError as exc:  # stale, not a reseller, nothing to undo, bad input
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, uid, form, where)
    return await _sheet_page(request, uid, form, notice=saved, notice_in=where)


async def resellers(request: web.Request) -> web.Response:
    raw = request.query.get("o", "0")
    offset = int(raw) if raw.isdigit() and len(raw) < 10 else 0
    async with request.app[SESSION_FACTORY]() as session:
        page = await list_resellers(session, offset=offset)
    return render(
        request,
        "resellers.html",
        page=page,
        empty_text=texts.admin_resellers_empty,
        words=_WORDS,
        list_url=RESELLERS_URL,
    )


async def reseller_sheet(request: web.Request) -> web.Response:
    return await _sheet_page(request, _uid(request), request.query)


async def price_set(request: web.Request) -> web.Response:
    async def write(session: AsyncSession, uid: int, form: Any) -> str:
        return await edit_plan_price(
            session,
            uid,
            slug=_field(form, "slug"),
            duration=_duration(_field(form, "d")),
            tier=_field(form, "t") or DEFAULT_TIER,
            raw=str(form.get("value", ""))[:64],
            seen=_seen(form),
        )

    return await _write(request, "plans", write)


async def price_clear(request: web.Request) -> web.Response:
    async def write(session: AsyncSession, uid: int, form: Any) -> str:
        return await clear_cell_price(
            session,
            uid,
            slug=_field(form, "slug"),
            duration=_duration(_field(form, "d")),
            tier=_field(form, "t") or DEFAULT_TIER,
            seen=_seen(form),
        )

    return await _write(request, "plans", write)


async def rate_set(request: web.Request) -> web.Response:
    field = request.match_info["field"]

    async def write(session: AsyncSession, uid: int, form: Any) -> str:
        return await edit_rate_field(
            session, uid, field=field, raw=str(form.get("value", ""))[:64], seen=_seen(form)
        )

    return await _write(request, "custom" if field in ("gb", "day") else "trial", write)


async def _cut_preview(request: web.Request, uid: int, form: Any) -> web.Response:
    """The first request: check the percent, show the bot's preview, write nothing."""
    refusal: ResellerError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            plan = await plan_cut(
                session,
                uid,
                category=_field(form, "c"),
                duration=_duration(_field(form, "d")),
                tier=_field(form, "t"),
                raw_pct=str(form.get("pct", ""))[:16],
            )
        except ResellerError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, uid, form, "plans")
    return render(
        request,
        "reseller_cut_confirm.html",
        preview=plan.text,
        digest=plan.digest,
        pct=plan.pct,
        category=_field(form, "c"),
        duration=_field(form, "d"),
        tier=_field(form, "t"),
        post_url=f"{RESELLERS_URL}/{uid}/cut",
        cancel_url=_reload_url(uid, form),
    )


async def cut(request: web.Request) -> web.Response:
    """Two requests on one route. Without ``confirm=1``: the preview, nothing written. With
    it: the set the preview showed is applied, or nothing if any of its prices moved."""
    uid = _uid(request)
    form = await request.post()
    if str(form.get("confirm", "")) != "1":
        return await _cut_preview(request, uid, form)

    async def write(session: AsyncSession, uid_: int, form_: Any) -> str:
        return await apply_planned_cut(
            session,
            uid_,
            category=_field(form_, "c"),
            duration=_duration(_field(form_, "d")),
            tier=_field(form_, "t"),
            raw_pct=str(form_.get("pct", ""))[:16],
            seen=_seen(form_),
        )

    async with request.app[SESSION_FACTORY]() as session:
        refusal: ResellerError | None = None
        saved = ""
        try:
            saved = await write(session, uid, form)
        except ResellerError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, uid, form, "plans")
    return await _sheet_page(request, uid, form, notice=saved, notice_in="plans")


async def undo(request: web.Request) -> web.Response:
    async def write(session: AsyncSession, uid: int, form: Any) -> str:
        return await undo_cut(
            session,
            uid,
            category=_field(form, "c"),
            duration=_duration(_field(form, "d")),
            tier=_field(form, "t"),
            seen=_seen(form),
        )

    return await _write(request, "plans", write)


routes = [
    web.get(RESELLERS_URL, resellers),
    web.get(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}", reseller_sheet),
    web.post(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}/price", price_set),
    web.post(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}/price/clear", price_clear),
    web.post(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}/rate/{{field}}", rate_set),
    web.post(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}/cut", cut),
    web.post(f"{RESELLERS_URL}/{{user_id:{NUMERIC_ID_PATTERN}}}/undo", undo),
]
