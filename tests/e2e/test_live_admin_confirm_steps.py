"""Two admin actions that apply at once now ask first: «📦 افزودن حجم» and «حذف نمایندگی»."""

from __future__ import annotations

import pytest

from tests.e2e._s07_helpers import db_user
from tests.e2e._s07b_helpers import open_card, tap_starting
from tests.e2e._s08_helpers import buy_active, open_service
from tests.e2e.harness import GB, App, Store, publish_store


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


def _cb(admin, label: str) -> str:  # type: ignore[no-untyped-def]
    hits = [b for row in admin.last_message.markup.inline_keyboard for b in row if label in b.text]
    assert len(hits) == 1, admin.last_message.rows
    return hits[0].callback_data


async def _is_reseller(app: App, who) -> bool:  # type: ignore[no-untyped-def]
    return bool((await db_user(app, who)).is_reseller)


# ------------------------------------------------------------------ «📦 افزودن حجم»


async def test_typed_gigabytes_wait_for_a_confirm_then_land_once(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    assert account.data_limit == 50 * GB
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📦 افزودن حجم")
    await admin.send("10")
    assert app.panel.user(account.username).data_limit == 50 * GB, "typing only asks"
    assert "10 گیگ" in admin.last_text and "50 گیگ" in admin.last_text, admin.last_text
    assert "60 گیگ" in admin.last_text, admin.last_text
    await tap_starting(admin, "✅ بله، اضافه کن")
    assert app.panel.user(account.username).data_limit == 60 * GB
    assert "60 گیگ" in admin.last_text


async def test_a_second_tap_on_the_confirm_adds_nothing(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📦 افزودن حجم")
    await admin.send("10")
    message = admin.last_message
    callback = _cb(admin, "بله، اضافه کن")
    assert len(callback.encode()) < 64
    await admin.press(callback, message=message)
    await admin.press(callback, message=message)  # double tap / stale button
    assert app.panel.user(account.username).data_limit == 60 * GB, "added once, not twice"


async def test_back_from_the_data_confirm_adds_nothing(app: App, store: Store) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    admin = app.admin()
    await open_service(admin, "ali")
    await tap_starting(admin, "📦 افزودن حجم")
    await admin.send("10")
    message = admin.last_message
    callback = _cb(admin, "بله، اضافه کن")
    await tap_starting(admin, "⬅️ کارت سرویس")
    assert app.panel.user(account.username).data_limit == 50 * GB
    await admin.press(callback, message=message)  # the old confirm after going back
    assert app.panel.user(account.username).data_limit == 50 * GB, "nothing pending any more"


# ------------------------------------------------------------------ «حذف نمایندگی»


async def test_removing_the_reseller_role_asks_first(app: App, store: Store) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await tap_starting(admin, "🏪 نماینده کردن")
    assert await _is_reseller(app, rez)
    await tap_starting(admin, "🏪 حذف نمایندگی")
    assert await _is_reseller(app, rez), "the first tap only asks"
    assert any("بله، حذف کن" in b for b in admin.buttons), admin.buttons
    assert any("⬅️ کارت مشتری" in b for b in admin.buttons), admin.buttons
    await tap_starting(admin, "⛔️ بله، حذف کن")
    assert not await _is_reseller(app, rez)
    assert any("🏪 نماینده کردن" in b for b in admin.buttons), "the customer card is shown again"


async def test_a_stale_removal_confirm_changes_nothing(app: App, store: Store) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await tap_starting(admin, "🏪 نماینده کردن")
    await tap_starting(admin, "🏪 حذف نمایندگی")
    message = admin.last_message
    callback = _cb(admin, "بله، حذف کن")
    await admin.press(callback, message=message)
    assert not await _is_reseller(app, rez)
    await admin.press(callback, message=message)  # stale: already removed
    assert not await _is_reseller(app, rez)
    assert admin.popup, "the stale tap says so"


async def test_granting_the_role_stays_one_tap(app: App, store: Store) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await tap_starting(admin, "🏪 نماینده کردن")
    assert await _is_reseller(app, rez)
