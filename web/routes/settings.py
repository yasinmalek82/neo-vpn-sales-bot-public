"""«⚙️ تنظیمات»: the settings home and its four sections.

* **🗓 هشدار و حذف سرویس** is read and write. Every write is a ``POST`` into one
  function of ``services/lifecycle_settings.py`` (the same ones the bot's typed
  prompt and mode buttons end in) and only for a store owner, as in the bot.
  A form carries the stamp of the row it was drawn from; a stale one is refused.
  Real deletion («روشن») is refused by the service without a fresh rehearsal,
  whoever asks, and asks once more on a confirm page that writes nothing.
  Running the rehearsal, previewing the list, approving it and deleting a
  service stay in the bot: there is no route for them.
* **📡 گروه گزارش, 🔌 اتصال پنل, 🎨 پس‌زمینهٔ QR** are read-only: no form, no
  POST route. The panel page holds no value from the connection settings.

A GET never changes anything; a route here holds no parse, range or state rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiohttp import web

from bot.texts import texts
from services.lifecycle_batches import current_batch, item_count
from services.lifecycle_settings import (
    NUMBER_FIELDS,
    LifecycleOwnerRequired,
    SettingsError,
    StaleLifecycleSettings,
    ask_delete_on,
    can_edit,
    current_stamp,
    load_config,
    save_typed_number,
    switch_delete_mode,
)
from services.lifecycle_view import lifecycle_view
from services.settings_overview import load_panel, load_qr, load_reports, overview
from services.ux_format import to_persian_digits
from web.keys import PANEL_PREFIX, SESSION_FACTORY, SETTINGS
from web.templating import render

SETTINGS_URL = f"{PANEL_PREFIX}/settings"
LIFECYCLE_URL = f"{SETTINGS_URL}/lifecycle"

DELETE_ON = "on"


def _form_stamp(form: object) -> str | None:
    value = form.get("stamp") if hasattr(form, "get") else None
    return str(value)[:64] if value else None


async def settings_home(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        cards = await overview(session, request.app[SETTINGS], base_url=SETTINGS_URL)
    return render(request, "settings_home.html", cards=cards)


async def settings_panel(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        view = await load_panel(session, request.app[SETTINGS], datetime.now(UTC))
    return render(request, "settings_panel.html", view=view, settings_url=SETTINGS_URL)


async def settings_reports(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        view = await load_reports(session)
    return render(request, "settings_reports.html", view=view, settings_url=SETTINGS_URL)


async def settings_qr(request: web.Request) -> web.Response:
    async with request.app[SESSION_FACTORY]() as session:
        view = await load_qr(session)
    return render(request, "settings_qr.html", view=view, settings_url=SETTINGS_URL)


# --------------------------------------------------------------------------- #
# 🗓 هشدار و حذف سرویس
# --------------------------------------------------------------------------- #


async def _lifecycle_page(
    request: web.Request,
    *,
    notice: str | None = None,
    notice_is_error: bool = False,
    notice_field: str | None = None,
    status: int = 200,
) -> web.Response:
    settings = request.app[SETTINGS]
    async with request.app[SESSION_FACTORY]() as session:
        config = await load_config(session, seed_days=settings.renewal_reminder_days)
        stamp = await current_stamp(session)
        waiting = await current_batch(session)
        batch_line = None
        if waiting is not None:
            count = await item_count(session, int(waiting.id))
            batch_line = texts.admin_life_batch_line.format(n=to_persian_digits(count)).strip()
    return render(
        request,
        "settings_lifecycle.html",
        status=status,
        view=lifecycle_view(config, datetime.now(UTC)),
        stamp=stamp,
        owner=can_edit(settings, request["telegram_id"]),
        not_owner=texts.admin_life_not_owner,
        batch_line=batch_line,
        notice=notice,
        notice_is_error=notice_is_error,
        notice_field=notice_field,
        settings_url=SETTINGS_URL,
        lifecycle_url=LIFECYCLE_URL,
    )


async def lifecycle_page(request: web.Request) -> web.Response:
    return await _lifecycle_page(request)


def _refused(request: web.Request, status: int, message: str) -> web.Response:
    return render(
        request, "store_refused.html", status=status, message=message, reload_url=LIFECYCLE_URL
    )


async def _refusal_page(
    request: web.Request, refusal: SettingsError, *, field: str | None
) -> web.Response:
    """The one mapping from a refused write to a page: owner 403, stale 409, input 400."""
    if isinstance(refusal, LifecycleOwnerRequired):
        return _refused(request, 403, str(refusal))
    if isinstance(refusal, StaleLifecycleSettings):
        return _refused(request, 409, str(refusal))
    return await _lifecycle_page(
        request,
        notice=texts.admin_life_input_error.format(reason=refusal),
        notice_is_error=True,
        notice_field=field,
        status=400,
    )


async def lifecycle_number(request: web.Request) -> web.Response:
    field = request.match_info["field"]
    if field not in NUMBER_FIELDS:
        return _refused(request, 404, "این تنظیم در پنل نیست.")
    form = await request.post()
    refusal: SettingsError | None = None
    saved = ""
    async with request.app[SESSION_FACTORY]() as session:
        try:
            saved = await save_typed_number(
                session,
                request.app[SETTINGS],
                request["telegram_id"],
                expected_stamp=_form_stamp(form),
                field=field,
                raw=str(form.get("value", "")),
            )
        except SettingsError as exc:  # owner, stale and invalid input all derive from it
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, field=field)
    return await _lifecycle_page(request, notice=saved, notice_field=field)


async def lifecycle_mode(request: web.Request) -> web.Response:
    """off and rehearsal apply at once, as in the bot. «روشن» asks first (nothing is
    written), then a second request with ``confirm=1`` applies it, and the service
    refuses it without a fresh rehearsal on both."""
    form = await request.post()
    mode = str(form.get("mode", ""))
    stamp = _form_stamp(form)
    settings = request.app[SETTINGS]
    refusal: SettingsError | None = None
    asking = mode == DELETE_ON and str(form.get("confirm", "")) != "1"
    already_on = False
    async with request.app[SESSION_FACTORY]() as session:
        try:
            if asking:
                already_on = await ask_delete_on(
                    session, settings, request["telegram_id"], expected_stamp=stamp
                )
            else:
                await switch_delete_mode(
                    session, settings, request["telegram_id"], expected_stamp=stamp, mode=mode
                )
        except SettingsError as exc:
            refusal = exc
    if refusal is not None:
        return await _refusal_page(request, refusal, field="mode")
    if asking and not already_on:
        return render(
            request,
            "settings_confirm.html",
            message=texts.admin_life_mode_confirm,
            confirm_label=texts.admin_life_mode_confirm_yes,
            post_url=f"{LIFECYCLE_URL}/mode",
            stamp=stamp,
            mode=DELETE_ON,
            cancel_url=LIFECYCLE_URL,
        )
    if asking:  # already on: nothing to confirm, the page is the answer
        return await _lifecycle_page(request)
    return await _lifecycle_page(
        request,
        notice=texts.admin_life_mode_set.format(mode=texts.admin_life_mode_names[mode]),
        notice_field="mode",
    )


routes = [
    web.get(SETTINGS_URL, settings_home),
    web.get(f"{SETTINGS_URL}/panel", settings_panel),
    web.get(f"{SETTINGS_URL}/reports", settings_reports),
    web.get(f"{SETTINGS_URL}/qr", settings_qr),
    web.get(LIFECYCLE_URL, lifecycle_page),
    web.post(f"{LIFECYCLE_URL}/number/{{field}}", lifecycle_number),
    web.post(f"{LIFECYCLE_URL}/mode", lifecycle_mode),
]
