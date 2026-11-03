"""«مشتری‌ها»: search, the customer card, and the wallet add or deduct.

Every read and the one write go through the service functions the bot's
customer desk calls (``services/customer_lookup.py``, ``customer_card.py``).
The wallet change is two steps and POST only: the form's POST checks the amount
and answers with a confirm page (nothing is written), the confirm page's POST
applies it under the request key the server minted for that page
(``services/wallet_request_key.py``). A GET never changes anything.

What the bot's card offers beyond this (cut, activate, ban, gift, renew, trial
reset, reseller role and prices, zeroing the wallet) has no route here, on
purpose: ``.claude/rules/web-panel.md``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from services.customer_card import card_facts, customer_name
from services.customer_lookup import (
    DIRECTION_ADD,
    DIRECTION_DEDUCT,
    SEARCH_LIMIT,
    USER_FILTER_LABELS,
    ManualAdjustError,
    UserFilter,
    WalletAdjustRefused,
    apply_wallet_adjustment,
    get_customer_card,
    list_users,
    parse_signed_toman,
    preview_wallet_adjustment,
    search_customer_rows,
)
from services.ux_format import fa_toman
from services.wallet import WalletError, get_balance
from services.wallet_request_key import mint_request_token, stamp_of_request_token
from web.keys import NUMERIC_ID_PATTERN, PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.templating import render

MAX_QUERY_CHARS = 200
TEXT_REPLAYED = "این درخواست قبلاً ثبت شده بود. چیزی دوباره اعمال نشد."

_FILTERS = [(f.value, USER_FILTER_LABELS[f]) for f in UserFilter]


def _user_id(request: web.Request) -> int:
    return int(request.match_info["user_id"])


def _not_found(request: web.Request) -> web.Response:
    response = render(request, "message.html", message=texts.admin_customer_not_found)
    response.set_status(404)
    return response


def _parse_filter(raw: str) -> UserFilter:
    try:
        return UserFilter(raw)
    except ValueError:
        return UserFilter.ALL


def _offset(raw: str) -> int:
    return int(raw) if raw.isdigit() and len(raw) < 10 else 0


async def customers(request: web.Request) -> web.Response:
    """The browse list (paged, filtered), or the search results when ``q`` is given."""
    query = request.query.get("q", "").strip()[:MAX_QUERY_CHARS]
    user_filter = _parse_filter(request.query.get("f", UserFilter.ALL.value))
    offset = _offset(request.query.get("o", "0"))
    async with request.app[SESSION_FACTORY]() as session:
        if query:
            rows = await search_customer_rows(session, query)
            page = None
        else:
            page = await list_users(session, offset=offset, user_filter=user_filter)
            rows = page.rows
    return render(
        request,
        "customers.html",
        q=query,
        rows=rows,
        page=page,
        filters=_FILTERS,
        current_filter=user_filter.value,
        search_limit=SEARCH_LIMIT,
        empty_text=(
            texts.admin_customer_none.format(query=query)
            if query
            else (
                texts.admin_users_empty
                if user_filter is UserFilter.ALL
                else texts.admin_users_filter_empty.format(label=USER_FILTER_LABELS[user_filter])
            )
        ),
    )


async def customer_card(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        try:
            card = await get_customer_card(session, _user_id(request))
        except ManualAdjustError:
            return _not_found(request)
    return render(
        request,
        "customer_card.html",
        facts=card_facts(card, web=True),
        user_id=_user_id(request),
        no_ledger=texts.admin_customer_ledger_empty,
    )


def _form_page(request: web.Request, **context: Any) -> web.Response:
    return render(request, "wallet_form.html", user_id=_user_id(request), **context)


async def _balance(session: AsyncSession, user_id: int) -> int | None:
    try:
        return await get_balance(session, user_id)
    except WalletError:  # an unknown id: the caller answers «not found»
        return None


async def wallet_form(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        balance = await _balance(session, _user_id(request))
    if balance is None:
        return _not_found(request)
    return _form_page(
        request, balance=fa_toman(balance), error=None, amount="", direction=DIRECTION_ADD
    )


async def wallet_confirm(request: web.Request) -> web.Response:
    """Check the form and show who, how much, before and after. Writes nothing."""
    form = await request.post()
    raw_amount = str(form.get("amount", ""))[:64]
    direction = str(form.get("direction", ""))
    uid = _user_id(request)
    async with request.app[SESSION_FACTORY]() as session:
        try:
            preview = await preview_wallet_adjustment(
                session, user_id=uid, raw_amount=raw_amount, direction=direction
            )
        except WalletAdjustRefused as refused:
            if refused.kind == "user_not_found":
                return _not_found(request)
            balance = await _balance(session, uid)
            if balance is None:
                return _not_found(request)
            return _form_page(
                request,
                balance=fa_toman(balance),
                error=str(refused),
                amount=raw_amount,
                direction=direction if direction == DIRECTION_DEDUCT else DIRECTION_ADD,
            )
        name = customer_name(preview.user)
    token = mint_request_token(
        request.app[SETTINGS].bot_token,
        admin=request["telegram_id"],
        user=uid,
        amount=preview.amount_toman,
    )
    return render(
        request,
        "wallet_confirm.html",
        user_id=uid,
        customer=name,
        is_credit=preview.is_credit,
        amount=fa_toman(abs(preview.amount_toman)),
        before=fa_toman(preview.balance_toman),
        after=fa_toman(preview.after_toman),
        signed_amount=preview.amount_toman,
        token=token,
    )


async def wallet_apply(request: web.Request) -> web.Response:
    """Apply one confirmed adjustment under the page's request key."""
    form = await request.post()
    uid = _user_id(request)
    amount = parse_signed_toman(str(form.get("amount", ""))[:64])
    stamp = (
        stamp_of_request_token(
            request.app[SETTINGS].bot_token,
            str(form.get("token", ""))[:128],
            admin=request["telegram_id"],
            user=uid,
            amount=amount,
        )
        if amount
        else None
    )
    if not amount or stamp is None:
        # Not a page this server rendered for this customer and amount.
        async with request.app[SESSION_FACTORY]() as session:
            balance = await _balance(session, uid)
        if balance is None:
            return _not_found(request)
        return _form_page(
            request,
            balance=fa_toman(balance),
            error=texts.stale_button,
            amount="",
            direction=DIRECTION_ADD,
        )
    async with request.app[SESSION_FACTORY]() as session:
        try:
            outcome = await apply_wallet_adjustment(
                session,
                user_id=uid,
                amount_toman=amount,
                admin_telegram_id=request["telegram_id"],
                stamp=stamp,
            )
        except ManualAdjustError as exc:
            balance = await _balance(session, uid)
            if balance is None:
                return _not_found(request)
            return _form_page(
                request,
                balance=fa_toman(balance),
                error=str(exc),
                amount="",
                direction=DIRECTION_ADD,
            )
        # The true balance now, not the page's "after": it may have moved since.
        balance = await _balance(session, uid) or 0
    return render(
        request,
        "message.html",
        message=texts.admin_wallet_done.format(wallet=fa_toman(balance)),
        note=None if outcome.created else TEXT_REPLAYED,
        back_user_id=uid,
    )


routes = [
    web.get(f"{PANEL_PREFIX}/customers", customers),
    web.get(f"{PANEL_PREFIX}/customers/{{user_id:{NUMERIC_ID_PATTERN}}}", customer_card),
    web.get(f"{PANEL_PREFIX}/customers/{{user_id:{NUMERIC_ID_PATTERN}}}/wallet", wallet_form),
    web.post(
        f"{PANEL_PREFIX}/customers/{{user_id:{NUMERIC_ID_PATTERN}}}/wallet/confirm", wallet_confirm
    ),
    web.post(
        f"{PANEL_PREFIX}/customers/{{user_id:{NUMERIC_ID_PATTERN}}}/wallet/apply", wallet_apply
    ),
]
