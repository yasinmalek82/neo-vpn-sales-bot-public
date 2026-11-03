"""Section 14: the panel screens in settings, and the node watcher that warns the owner.

Rows ۱۴.۱ to ۱۴.۸.
"""

from __future__ import annotations

import pytest

from bot.texts import texts
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s14_helpers import (
    add_system_load,
    age_outage,
    open_panel_page,
    right_key,
    set_api_key,
    system_texts,
    watch,
)
from tests.e2e.harness import GB, App, Store, digits, plain, publish_store, smoke


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


@smoke("۱۴.۱")
async def test_an_admin_who_is_not_the_owner_sees_the_page_but_cannot_edit_it(
    app: App, store: Store
) -> None:
    helper = app.admin(1)
    await open_panel_page(helper)
    assert "تنظیمات پنل PasarGuard" in helper.last_text
    assert plain(texts.admin_panel_view_only) in helper.last_text
    edit_buttons = [
        b
        for b in helper.buttons
        if any(w in b for w in ("آدرس", "API", "مسیر", "تایم", "دستگاه تست", ".env"))
    ]
    assert edit_buttons == [], edit_buttons
    assert "📊 وضعیت پنل" in helper.buttons and "📋 لیست گروه‌های پنل" in helper.buttons

    owner = app.admin(0)
    await open_panel_page(owner)
    assert "🔑 تنظیم API Key" in owner.buttons and "♻️ بازگشت به پیش‌فرض .env" in owner.buttons
    assert plain(texts.admin_panel_view_only) not in owner.last_text


@smoke("۱۴.۱")
async def test_a_forged_edit_tap_from_the_other_admin_is_refused(app: App, store: Store) -> None:
    owner, helper = app.admin(0), app.admin(1)
    await open_panel_page(owner)
    key_button = next(
        b.callback_data
        for row in owner.last_message.markup.inline_keyboard
        for b in row
        if b.text.startswith("🔑")
    )
    await open_panel_page(helper)
    await helper.press(key_button)
    assert helper.popup == texts.admin_store_owner_only
    assert plain(texts.admin_panel_prompt_api_key) not in helper.last_text


@smoke("۱۴.۲")
async def test_a_wrong_key_saves_with_a_warning_and_the_right_key_says_connected(
    app: App, store: Store
) -> None:
    owner = app.admin(0)
    await open_panel_page(owner)
    await set_api_key(owner, "pg_key_not_the_right_one")
    assert owner.last_text.startswith("⚠️ ذخیره شد ولی اتصال برقرار نیست"), owner.last_text
    await set_api_key(owner, right_key())
    assert owner.last_text.startswith("✅ ذخیره شد — اتصال برقرار است"), owner.last_text
    assert "pg_key_" not in owner.last_text, "the key is never shown back"


@smoke("۱۴.۲")
async def test_the_wrong_key_warning_does_not_show_a_class_name(app: App, store: Store) -> None:
    owner = app.admin(0)
    await open_panel_page(owner)
    await set_api_key(owner, "pg_key_not_the_right_one")
    first_line = owner.last_text.splitlines()[0]
    assert "PasarGuardAuthError" not in first_line, first_line
    assert first_line.endswith("پنل کلید API را قبول نکرد"), first_line


@smoke("۱۴.۳")
async def test_the_group_list_shows_users_inbounds_and_the_store_group_it_feeds(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await buy_active(app, store, ali)  # one user on panel group 2 «tunnel»
    owner = app.admin(0)
    await open_panel_page(owner)
    page = owner.last_message
    await tap_starting(owner, "لیست گروه")
    assert owner.last_message.message_id == page.message_id, "the same message changed"
    lines = owner.last_text.splitlines()
    assert digits(lines[0]) == "گروه‌های زنده پنل (3):", lines[0]
    by_name = {ln.split(" • ")[0]: ln for ln in lines[1:]}
    assert by_name["#1 direct"].endswith("0 کاربر • 0 اینباند → نامحدود"), by_name
    assert "1 کاربر" in by_name["#2 tunnel"] and by_name["#2 tunnel"].endswith("→ ویژه")
    assert by_name["#3 premium"].endswith("→ اقتصادی")


@smoke("۱۴.۴")
async def test_the_status_screen_shows_version_load_users_traffic_and_every_server(
    app: App, store: Store
) -> None:
    add_system_load(app, cpu=37.0, cores=4, mem_used=GB, mem_total=4 * GB)
    app.panel.add_node(2, "node-2", status="error", message="xray crashed")
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    owner = app.admin(0)
    await open_panel_page(owner)
    page = owner.last_message
    await tap_starting(owner, "وضعیت پنل")
    text = owner.last_text
    assert owner.last_message.message_id == page.message_id
    assert "نسخه: 1.0.0-fake" in text
    assert "پردازنده: 37٪ (4 هسته)" in text and "رم: " in text
    for label in ("کل: 1", "فعال: 1", "آنلاین الان", "منقضی", "غیرفعال", "دریافتی", "ارسالی"):
        assert label in text, f"{label!r} missing:\n{text}"
    servers = text.split("🌐 سرورها")[1]
    assert "🟢 node-1" in servers and "🔴 node-2" in servers


@smoke("۱۴.۴")
async def test_no_button_on_the_status_screen_has_a_doubled_arrow(app: App, store: Store) -> None:
    owner = app.admin(0)
    await open_panel_page(owner)
    await tap_starting(owner, "وضعیت پنل")
    doubled = [b for b in owner.buttons if "⬅️ ⬅️" in b]
    assert doubled == [], doubled


@smoke("۱۴.۵")
async def test_reconnecting_a_red_server_sends_the_request_and_refreshes_the_page(
    app: App, store: Store
) -> None:
    owner = app.admin(0)
    await open_panel_page(owner)
    await tap_starting(owner, "وضعیت پنل")
    assert not [b for b in owner.buttons if "وصل دوباره" in b], "no red server, no button"

    app.panel.add_node(2, "node-2", status="error", message="xray crashed")
    await tap_starting(owner, "به‌روزرسانی")
    page = owner.last_message
    assert "🔴 node-2" in owner.last_text
    await tap_starting(owner, "🔄 وصل دوباره: node-2")
    assert [r.path for r in app.panel.requests_to("POST /api/node/{node_id}/reconnect")] == [
        "/api/node/2/reconnect"
    ]
    assert owner.last_text.startswith("✅ درخواست اتصال دوباره فرستاده شد")
    assert owner.last_message.message_id == page.message_id and "🟢 node-1" in owner.last_text


@smoke("۱۴.۶")
async def test_a_node_that_stays_down_gives_one_alert_and_a_warning_on_the_home_screen(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    owner = app.admin(0)
    await watch(app)  # the first look: all well
    app.panel.set_node(1, "error", "xray crashed")

    await watch(app)
    assert await system_texts(group) == [], "one bad look is not an alert"
    await owner.send("/admin")
    assert "⚠️" not in owner.last_text

    await watch(app)  # the second look
    alerts = await system_texts(group)
    assert len(alerts) == 1 and "سرور «node-1» قطع شد" in alerts[0], alerts
    assert "xray crashed" in alerts[0]
    await owner.send("/admin")
    assert "⚠️ سرور «node-1» قطع است" in owner.last_text, owner.last_text

    await watch(app, 3)  # it stays down: nothing more is said
    assert len(await system_texts(group)) == 1


@smoke("۱۴.۷")
async def test_a_node_that_comes_back_is_announced_once_after_two_good_looks(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    owner = app.admin(0)
    await watch(app)
    app.panel.set_node(1, "error", "xray crashed")
    await watch(app, 2)
    await age_outage(app, minutes=12)
    app.panel.set_node(1, "connected")

    await watch(app)  # the first good look
    assert len(await system_texts(group)) == 1, "one good look is not yet «وصل شد»"
    await owner.send("/admin")
    assert "⚠️" not in owner.last_text, "the warning leaves the home screen at once"

    await watch(app)  # the second good look
    messages = await system_texts(group)
    assert len(messages) == 2 and "دوباره وصل شد" in messages[1], messages
    assert "مدت قطعی: 12 دقیقه" in messages[1]

    await watch(app, 2)
    assert len(await system_texts(group)) == 2, "and never again"


@smoke("۱۴.۷")
async def test_a_flap_down_up_down_sends_one_down_alert_and_no_recovery(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    await watch(app)
    app.panel.set_node(1, "error", "xray crashed")
    await watch(app, 2)
    app.panel.set_node(1, "connected")
    await watch(app)  # one good look
    app.panel.set_node(1, "error", "xray crashed")
    await watch(app, 3)
    messages = await system_texts(group)
    assert [("قطع شد" in m, "وصل شد" in m) for m in messages] == [(True, False)], messages


@smoke("۱۴.۸")
async def test_a_node_the_owner_disabled_in_the_panel_makes_no_alert(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    owner = app.admin(0)
    await watch(app)
    app.panel.set_node(1, "disabled")
    await watch(app, 4)
    assert await system_texts(group) == []
    await owner.send("/admin")
    assert "⚠️" not in owner.last_text
    app.panel.set_node(1, "connected")
    await watch(app, 3)
    assert await system_texts(group) == [], "and its return is not announced as a recovery"
