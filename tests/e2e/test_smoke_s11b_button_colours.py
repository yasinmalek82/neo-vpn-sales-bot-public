"""Section ۱۱ب: button colour and layout.

Rows ۱۱ب.۱ to ۱۱ب.۸.
MANUAL: the look on a phone (۱۱ب.۱ says «با موبایل»): the fake client has no renderer, so these
read the ``style`` field and the row layout the bot sends, which is what Telegram draws from.
STALE: ۱۱ب.۷ names the red «🚫 مسدود کردن» on the customer card; the card's switch
(«🚫 مسدود کردن» / «✅ رفع مسدودی») has no colour, and the red «🚫 مسدود کردن» sits on «👥 مشتری‌ها».
Both carry the same words, so the test tells them apart by screen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.e2e._s01_helpers import app_with
from tests.e2e._s07b_helpers import approve_order, open_card, tap_starting
from tests.e2e._s08_helpers import buy_active, expire_in, my_service
from tests.e2e._s09_helpers import (
    bottom_keyboard,
    coloured,
    every_keyboard_sent,
    rows_of,
    styled_buttons,
)
from tests.e2e.harness import App, Store, order_by_card, publish_store, smoke

BUY = "🛒 خرید اشتراک"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


def _layout_ok(rows: list[list[str]], primary: str) -> None:
    assert rows[0] == [primary], f"the primary action is not alone on row 1: {rows}"
    assert all(len(r) <= 2 for r in rows[1:]), f"more than two per row: {rows}"


@smoke("۱۱ب.۱")
async def test_the_menu_has_buy_alone_on_the_first_row_and_the_rest_in_pairs(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    rows = rows_of(bottom_keyboard(app))
    _layout_ok(rows, BUY)
    assert rows[1] == ["🎟 تست رایگان", "📡 سرویس‌های من"]
    # The inline copy of the menu, reached from «بازگشت به منو», has the same shape.
    await ali.tap_reply("🎁 دعوت دوستان")
    await ali.tap("⬅️ بازگشت به منو")
    _layout_ok(rows_of(ali.last_message.markup), BUY)


@smoke("۱۱ب.۱")
async def test_the_bottom_menu_colours_buy_more_than_the_rest(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    styles = styled_buttons(bottom_keyboard(app))
    assert styles[BUY] == "primary", styles


@smoke("۱۱ب.۲")
async def test_the_inline_menu_has_exactly_one_coloured_button(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply("🎁 دعوت دوستان")
    await ali.tap("⬅️ بازگشت به منو")
    assert coloured(ali.last_message.markup) == [BUY]


@smoke("۱۱ب.۲")
async def test_the_bottom_menu_has_exactly_one_coloured_button(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    assert coloured(bottom_keyboard(app)) == [BUY]


async def _service_card(app: App, store: Store, *, days: float):  # type: ignore[no-untyped-def]
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    expire_in(app.panel.user(account.username), days=days)
    await my_service(ali)
    return ali


@smoke("۱۱ب.۳")
async def test_an_active_far_from_expiry_service_leads_with_the_connection_guide(
    app: App, store: Store
) -> None:
    ali = await _service_card(app, store, days=20)
    rows = rows_of(ali.last_message.markup)
    assert rows[0] == ["📖 راهنمای اتصال"]
    assert coloured(ali.last_message.markup) == ["📖 راهنمای اتصال"]


@smoke("۱۱ب.۴")
async def test_a_service_about_to_expire_leads_with_renew_and_the_colour_follows(
    app: App, store: Store
) -> None:
    ali = await _service_card(app, store, days=2)
    assert "به‌زودی منقضی می‌شه" in ali.last_text
    rows = rows_of(ali.last_message.markup)
    assert rows[0] == ["📅 تمدید این سرویس"], rows
    assert coloured(ali.last_message.markup) == ["📅 تمدید این سرویس"]
    assert "📖 راهنمای اتصال" in [
        b for r in rows for b in r
    ], "the guide is still there, uncoloured"


@smoke("۱۱ب.۵")
async def test_back_is_alone_on_the_last_row_and_change_link_is_not_beside_it(
    app: App, store: Store
) -> None:
    ali = await _service_card(app, store, days=2)
    rows = rows_of(ali.last_message.markup)
    assert rows[-1] == ["⬅️ بازگشت به سرویس‌ها"], rows
    assert ["🔐 تغییر لینک"] in rows, "the link button has a row of its own"
    assert not any("🔐 تغییر لینک" in r and len(r) > 1 for r in rows)


@smoke("۱۱ب.۶")
async def test_the_receipt_message_the_owner_gets_has_green_approve_and_red_reject(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    review = next(
        m
        for m in reversed(admin.messages)
        if m.markup is not None and "✅ تایید" in [b for r in m.rows for b in r]
    )
    styles = styled_buttons(review.markup)
    assert styles["✅ تایید"] == "success" and styles["❌ رد"] == "danger", styles


@smoke("۱۱ب.۶")
async def test_the_pending_receipt_opened_from_the_queue_has_green_and_red_too(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "پرداخت‌ها")
    await tap_starting(admin, "#1")
    styles = styled_buttons(admin.last_message.markup)
    assert styles["✅ تایید"] == "success" and styles["❌ رد"] == "danger", styles


@smoke("۱۱ب.۷")
async def test_blocking_is_red_on_the_people_page_and_the_card_switch_has_no_colour(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "👥 مشتری‌ها")
    assert styled_buttons(admin.last_message.markup)["🚫 مسدود کردن"] == "danger"
    await open_card(admin, "ali")
    assert "🚫 مسدود کردن" in admin.buttons
    assert not styled_buttons(admin.last_message.markup)[
        "🚫 مسدود کردن"
    ], "the card's switch is plain"
    assert ali


@pytest.fixture
async def plain_app(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[App]:
    async for running in app_with(
        migrated_template, tmp_path, monkeypatch, button_colors_enabled=False
    ):
        yield running


@smoke("۱۱ب.۸")
async def test_with_colours_switched_off_no_button_has_a_colour_but_everything_still_works(
    plain_app: App,
) -> None:
    app = plain_app
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    bought = await buy_active(app, store, ali)  # the whole money path through taps
    assert bought.group_ids == [2]
    expire_in(app.panel.user(bought.username), days=2)
    await my_service(ali)
    assert (
        ali.buttons[:2] == ["📅 تمدید این سرویس", "📖 راهنمای اتصال"]
        or "📅 تمدید این سرویس" in ali.buttons
    )
    await ali.tap_reply("🎁 دعوت دوستان")
    await ali.tap("⬅️ بازگشت به منو")
    assert BUY in ali.buttons
    carol = app.customer("carol")
    await carol.start()
    await order_by_card(carol, store, "economy_50", "carol12")
    await carol.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "پرداخت‌ها")
    await tap_starting(admin, "#2")
    assert "✅ تایید" in admin.buttons and "❌ رد" in admin.buttons
    await approve_order(app, 2)
    await open_card(admin, "ali")
    await admin.send("/admin")
    await tap_starting(admin, "👥 مشتری‌ها")
    assert "🚫 مسدود کردن" in admin.buttons
    keyboards = every_keyboard_sent(app)
    assert len(keyboards) > 20
    leaked = [(b, s) for k in keyboards for b, s in styled_buttons(k).items() if s]
    assert leaked == [], f"colour survived BUTTON_COLORS_ENABLED=false: {leaked[:5]}"
