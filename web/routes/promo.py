"""«🏷 کدهای تخفیف»: the codes with their usage rules, and the bot's three writes.

The list is the bot's list (``services/promo_admin.promo_lines`` composes it for both).
A code is created, switched on or off, or deleted through the same functions the bot's
wizard and buttons call (``save_promo_code``, ``toggle_promo_code``, ``delete_promo_code``),
each inside ``guarded_store_write``: a store owner only, and only against the published
store version the page was drawn from, so a stale or double-submitted form is refused.
Checkout stays the one place that refuses an inactive code. A GET never writes, and a
route here holds no percent, limit or date rule.

Creating a code that already exists replaces it (rules and percent) and keeps its
count, as the bot's wizard does; that is how a code is edited.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from services.promo_admin import (
    answers_from_form,
    assert_promo_listed,
    delete_promo_code,
    parse_code_and_percent,
    promo_lines,
    save_promo_code,
    toggle_promo_code,
)
from services.store_admin import (
    StoreAdminError,
    StoreOwnerRequired,
    get_catalog_view,
)
from services.store_config import is_store_owner
from services.store_edit import StaleStoreVersion, guarded_store_write, published_store_version
from services.store_strategy import list_promo_codes
from services.ux_format import to_persian_digits
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.templating import render

PROMO_URL = f"{PANEL_PREFIX}/promo"
SAVED = f"✅ {texts.admin_store_saved}"

_TAGS = re.compile(r"</?code>")


def _plain(text: str) -> str:
    """The bot sends HTML; a few of its lines carry <code>. Jinja escapes, so drop the tags."""
    return _TAGS.sub("", text)


def _form_version(form: Any) -> int | None:
    try:
        return int(str(form.get("version", "")))
    except ValueError:
        return None


async def _page(
    request: web.Request,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    typed: Any = None,
    status: int = 200,
) -> web.Response:
    owner = is_store_owner(request.app[SETTINGS], request["telegram_id"])
    async with request.app[SESSION_FACTORY]() as session:
        view = await get_catalog_view(session)
        version = await published_store_version(session)
        if not view.exists or view.version is None:
            return render(request, "promo.html", empty=texts.admin_store_no_catalog, status=status)
        promos = list_promo_codes(view.version.pricing_document)
        lines = await promo_lines(session, promos)
    title = texts.web_promo_count.format(count=to_persian_digits(len(promos)))
    return render(
        request,
        "promo.html",
        status=status,
        empty=None,
        title=title,
        read_only_hint=None if owner else texts.admin_store_promo_ops_hint,
        lines=lines,
        can_edit=owner,
        version=version,
        notice=notice,
        notice_is_error=notice_is_error,
        typed=typed or {},
        promo_url=PROMO_URL,
        ask_first=texts.admin_promo_ask_first_only,
        ask_user_limit=texts.web_promo_ask_user_limit,
        ask_total_limit=texts.web_promo_ask_total_limit,
        ask_days=texts.web_promo_ask_days,
        rules_none=texts.admin_promo_rules_none,
    )


def _refused(request: web.Request, status: int, message: str) -> web.Response:
    return render(
        request,
        "store_refused.html",
        status=status,
        message=_plain(message),
        reload_url=PROMO_URL,
    )


async def _refusal_page(request: web.Request, refusal: StoreAdminError, form: Any) -> web.Response:
    """The one mapping from a refused write to a page: owner 403, stale 409, input 400."""
    if isinstance(refusal, StoreOwnerRequired):
        return _refused(request, 403, texts.admin_store_owner_only)
    if isinstance(refusal, StaleStoreVersion):
        return _refused(request, 409, str(refusal))
    return await _page(
        request,
        notice=_plain(str(refusal) or texts.admin_store_invalid_input),
        notice_is_error=True,
        typed=form,
        status=400,
    )


async def _write(request: web.Request, form: Any, action: Any) -> web.Response:
    refusal: StoreAdminError | None = None
    async with request.app[SESSION_FACTORY]() as session:
        try:
            await guarded_store_write(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                action=action,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, form)
    return await _page(request, notice=SAVED)


async def promo_home(request: web.Request) -> web.Response:
    return await _page(request)


async def promo_add(request: web.Request) -> web.Response:
    form = await request.post()

    async def action(s: AsyncSession) -> None:
        code, percent = parse_code_and_percent(
            str(form.get("code", "")), str(form.get("percent", ""))
        )
        answers = answers_from_form(
            first_purchase_only=str(form.get("first", "")) == "1",
            per_user=str(form.get("per_user", "")),
            total=str(form.get("total", "")),
            days=str(form.get("days", "")),
        )
        await save_promo_code(s, code=code, percent=percent, answers=answers)

    return await _write(request, form, action)


async def promo_toggle(request: web.Request) -> web.Response:
    form = await request.post()
    code = request.match_info["code"]

    async def action(s: AsyncSession) -> None:
        await toggle_promo_code(s, code)

    return await _write(request, form, action)


async def promo_delete(request: web.Request) -> web.Response:
    """Two steps, as the bot's: the first draws the bot's sentence, the second deletes."""
    form = await request.post()
    code = request.match_info["code"]
    if str(form.get("confirm", "")) == "1":

        async def action(s: AsyncSession) -> None:
            await delete_promo_code(s, code)

        return await _write(request, form, action)

    refusal: StoreAdminError | None = None

    async def preview(s: AsyncSession) -> None:
        await assert_promo_listed(s, code)
        await s.rollback()  # asking changes nothing: not even a draft is left behind

    async with request.app[SESSION_FACTORY]() as session:
        try:
            await guarded_store_write(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_version=_form_version(form),
                action=preview,
            )
        except StoreAdminError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, form)
    return render(
        request,
        "store_confirm.html",
        message=texts.admin_store_promo_del_confirm.format(code=code.strip().upper()),
        version=_form_version(form),
        post_url=request.path,
        cancel_url=PROMO_URL,
    )


routes = [
    web.get(PROMO_URL, promo_home),
    web.post(f"{PROMO_URL}/add", promo_add),
    web.post(f"{PROMO_URL}/c/{{code}}/toggle", promo_toggle),
    web.post(f"{PROMO_URL}/c/{{code}}/delete", promo_delete),
]
