"""Sections ۰ and ۱: before Telegram, and connecting the panel and the report group.

Rows ۰.۱ ۰.۲ ۰.۳ ۱.۱ ۱.۲ ۱.۳ ۱.۴ ۱.۵.
MANUAL: ۰.۱ (filling the real ``.env``: only the variable names and parsing are proved here),
۰.۴ (a second process on the same token is Telegram's ``409 Conflict``; the fake Telegram has
no second poller). STALE: ۱.۱ (the success line is only «اتصال برقرار است»; the panel
version moved to «📊 وضعیت پنل»), ۱.۳ (no «گروه‌های پیش‌فرض» screen any more: a store group
holds its own panel group, see ۹.۱۲), ۱.۴ and ۱.۵ (the group gets 8 topics, not 5).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from bot.config import Settings
from bot.texts import texts
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s14_helpers import open_panel_page
from tests.e2e.harness import App, plain, smoke
from tests.e2e.harness.settings import _declared_env_names

ROOT = Path(__file__).resolve().parents[2]
TOPICS = 8


@smoke("۰.۱")
def test_every_env_variable_the_checklist_names_is_read_by_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _declared_env_names():
        monkeypatch.delenv(name, raising=False)
    given = {
        "BOT_TOKEN": "111111111:AAexample-token-for-the-test",
        "ADMIN_TELEGRAM_IDS": "424242",
        "PASARGUARD_BASE_URL": "https://panel.example.test",
        "PASARGUARD_API_KEY": "key-for-the-test",
        "PAYMENT_CARD_NUMBER": "6037000011112222",
        "PAYMENT_CARD_HOLDER_NAME": "نام آزمون",
        "SUPPORT_HANDLE": "@support_test",
        "CHANNEL_HANDLE": "@channel_test",
    }
    for name, value in given.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.bot_token == given["BOT_TOKEN"]
    assert s.admin_telegram_ids == [424242]
    assert s.pasar_guard_base_url == given["PASARGUARD_BASE_URL"]
    assert s.pasar_guard_api_key == given["PASARGUARD_API_KEY"]
    assert s.payment_card_number == given["PAYMENT_CARD_NUMBER"]
    assert s.payment_card_holder_name == given["PAYMENT_CARD_HOLDER_NAME"]
    assert s.support_handle == given["SUPPORT_HANDLE"]
    assert s.channel_handle == given["CHANNEL_HANDLE"]


@smoke("۰.۱")
def test_a_missing_bot_token_stops_the_bot_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    for name in _declared_env_names():
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="bot_token|BOT_TOKEN"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@smoke("۰.۲")
async def test_the_database_is_at_the_head_of_the_migration_chain(app: App) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"two migration heads: {heads}"
    async with app.session() as session:
        version = (await session.execute(text("SELECT version_num FROM alembic_version"))).all()
        tables = {
            r[0]
            for r in (
                await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    assert [v[0] for v in version] == heads
    assert {
        "users",
        "orders",
        "wallet_ledger_entries",
        "report_outbox",
        "store_categories",
    } <= tables


@smoke("۰.۳")
async def test_startup_publishes_commands_and_turns_the_scheduler_on(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    import bot.main as bot_main
    from scheduler import get_scheduler

    caplog.set_level(logging.INFO)
    await bot_main.on_startup(app.bot)
    try:
        scheduler = get_scheduler()
        assert scheduler is not None and scheduler.running  # type: ignore[attr-defined]
        assert {
            "expire_stale_orders",
            "report_flush",
            "broadcast_worker",
            "cleanup",
            "nightly_digest",
            "renewal_reminders",
            "panel_watch",
            "database_backup",
        } <= set(
            scheduler.jobs
        )  # type: ignore[attr-defined]
    finally:
        await bot_main.on_shutdown(app.bot)
    log = caplog.text
    assert "Bot started" in log and "Scheduler started" in log
    commands = {c.command for m in app.tg.commands_set for c in m.commands}
    assert {"start", "admin", "cancel"} <= commands
    admin_scopes = [
        m.scope.chat_id
        for m in app.tg.commands_set
        if m.scope is not None and getattr(m.scope, "chat_id", None) is not None
    ]
    assert sorted(admin_scopes) == sorted(app.settings.admin_telegram_ids)
    assert "DeleteWebhook" in [c.method for c in app.tg.calls], "polling mode clears the webhook"


@smoke("۱.۱")
async def test_connection_test_says_connected_when_the_panel_answers(app: App) -> None:
    admin = app.admin()
    await open_panel_page(admin)
    await tap_starting(admin, "🔌 تست اتصال")
    assert admin.last_text.startswith(plain(texts.admin_panel_test_ok))
    assert app.panel.requests_to("GET /api/admin"), "the test must really call the panel"
    await tap_starting(admin, "📊 وضعیت پنل")
    assert "نسخه: 1.0.0-fake" in admin.last_text  # the version lives on the status page now


@smoke("۱.۱")
async def test_connection_test_says_why_when_the_panel_refuses_or_is_down(app: App) -> None:
    admin = app.admin()
    await open_panel_page(admin)
    app.panel.reject_key()
    await tap_starting(admin, "🔌 تست اتصال")
    assert plain(texts.admin_panel_test_ok) not in admin.last_text
    assert "اتصال برقرار نیست" in admin.last_text, admin.last_text
    assert "pg_key_e2e_test_key" not in admin.last_text
    assert "Traceback" not in admin.last_text and "Error(" not in admin.last_text
    assert admin.last_text.splitlines()[0] == "⚠️ اتصال برقرار نیست: پنل کلید API را قبول نکرد"
    app.panel.go_down()
    await tap_starting(admin, "🔌 تست اتصال")
    first = admin.last_text.splitlines()[0]
    assert first.startswith("⚠️ اتصال برقرار نیست: "), first
    assert not re.search(r"[A-Za-z]{4,}", first), f"an English reason reached the owner: {first}"
    assert plain(texts.admin_panel_test_ok) not in admin.last_text
    assert "pg_key_e2e_test_key" not in admin.last_text
    assert admin.buttons, "a failed test still leaves the owner a keyboard"


@smoke("۱.۲")
async def test_the_panels_real_groups_are_listed_with_their_ids(app: App) -> None:
    admin = app.admin()
    await open_panel_page(admin)
    await tap_starting(admin, "📋 لیست گروه")
    assert "#1 direct" in admin.last_text
    assert "#2 tunnel" in admin.last_text
    assert "#3 premium" in admin.last_text
    assert "(3)" in admin.last_text or "۳" in admin.last_text


@smoke("۱.۳")
async def test_there_is_no_default_group_screen_any_more_only_the_store_group_holds_one(
    app: App,
) -> None:
    admin = app.admin()
    await open_panel_page(admin)
    assert not any("پیش‌فرض" in b and ".env" not in b for b in admin.buttons), admin.buttons
    assert "گروه پنلِ هر گروه فروشگاه" in admin.last_text


async def _open_report_screen(admin) -> None:  # type: ignore[no-untyped-def]
    await admin.send("/admin")
    await tap_starting(admin, "⚙️")
    await tap_starting(admin, "📡")


@smoke("۱.۴")
async def test_connecting_the_group_makes_every_topic_and_turns_reporting_on(app: App) -> None:
    admin = app.admin()
    await _open_report_screen(admin)
    assert plain(texts.admin_reports_not_attached) in admin.last_text
    await tap_starting(admin, "🔗 وصل کردن گروه")
    chat = app.tg.add_forum(-1001234567890)
    await admin.send("-1001234567890")
    assert "آماده شد" in admin.last_text
    assert len(chat.topics) == TOPICS, chat.topics
    assert admin.last_text.count("ساخته و تست شد") == TOPICS
    await _open_report_screen(admin)
    assert "تاپیک آماده: 8 از 8" in admin.last_text
    assert "✅ روشن" in admin.last_text
    # Reconnecting the same group does not make a second set of topics.
    await tap_starting(admin, "🔗 تغییر گروه")
    await admin.send("-1001234567890")
    assert len(chat.topics) == TOPICS, "a second connect duplicated the topics"
    assert admin.last_text.count("از قبل بود") == TOPICS


@smoke("۱.۵")
async def test_the_test_button_of_every_topic_puts_a_real_message_in_that_topic(
    app: App,
) -> None:
    admin = app.admin()
    group = await app.connect_report_group()
    keys = list(await group._threads())  # noqa: SLF001
    assert len(keys) == TOPICS
    before = {k: len(await group.messages(k)) for k in keys}
    await _open_report_screen(admin)
    message = admin.last_message
    tests = [
        b.callback_data
        for row in message.markup.inline_keyboard  # type: ignore[union-attr]
        for b in row
        if b.callback_data and b.callback_data.startswith("adm:rep:v:")
    ]
    assert len(tests) == TOPICS
    for data in tests:
        await admin.press(data, message=admin.last_message)
    for key in keys:
        messages = await group.messages(key)
        assert len(messages) == before[key] + 1, f"topic {key} got no new test message"
        assert "تست مسیر گزارش" in messages[-1].text


@smoke("۱.۵")
async def test_a_topic_whose_test_message_cannot_land_is_no_longer_ready(app: App) -> None:
    app.tg.accept("no_thread")
    admin = app.admin()
    group = await app.connect_report_group()
    threads = await group._threads()  # noqa: SLF001
    await _open_report_screen(admin)
    assert "تاپیک آماده: 8 از 8" in admin.last_text
    group.chat.topics.pop(threads["wallet"])  # the owner deleted the topic in Telegram
    message = admin.last_message
    await admin.press("adm:rep:v:wallet", message=message)
    assert admin.popup, "a failed test must say so"
    await _open_report_screen(admin)
    assert "تاپیک آماده: 7 از 8" in admin.last_text, admin.last_text
    assert "🟡 🏦 کیف پول" in " ".join(admin.buttons) or "🟡" in " ".join(admin.buttons)
