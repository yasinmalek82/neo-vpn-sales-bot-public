"""Section 2, the rows ``test_smoke_s02_first_contact.py`` does not cover.

Covered here: ۲.۲ ۲.۷ ۲.۸ ۲.۹ ۲.۱۰ ۲.۱۱ ۲.۱۲ ۲.۱۳ ۲.۱۴.
MANUAL: ۲.۳ (the bottom keyboard staying put while a real phone scrolls) needs a
real Telegram client; the fake keeps the keyboard until a message replaces it, and
the other scenarios already read it after many messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat
from sqlalchemy import select

from bot.commands import setup_bot_commands
from bot.keyboards.reply_menu import SUPPORT
from bot.texts import texts
from database.models import DisruptionReport
from database.models.report import ReportSettings
from tests.e2e._s01_helpers import app_with, open_admin_panel, report_disruption
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App, digits, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.settings import SUPPORT_HANDLE, SUPPORT_HOURS

GROUP_CHAT_ID = -1001234567890


@smoke("۲.۲")
async def test_the_command_menu_lists_start_and_cancel_and_admin_only_for_admins(
    app: App,
) -> None:
    """Startup publishes the blue Menu button's list: this is the call ``on_startup`` makes."""
    await setup_bot_commands(app.bot, app.settings.admin_telegram_ids)
    published = {
        type(call.scope).__name__
        + (f":{call.scope.chat_id}" if hasattr(call.scope, "chat_id") else ""): [
            c.command for c in call.commands
        ]
        for call in app.tg.commands_set
    }
    customers = published[BotCommandScopeAllPrivateChats.__name__]
    assert customers == ["start", "cancel"], customers
    for admin_id in app.settings.admin_telegram_ids:
        assert published[f"{BotCommandScopeChat.__name__}:{admin_id}"] == [
            "start",
            "admin",
            "cancel",
        ]


@smoke("۲.۷")
async def test_support_screen_shows_the_handle_and_the_hours(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(SUPPORT)
    assert report_plain(f"{SUPPORT_HANDLE} ({SUPPORT_HOURS})") in ali.last_text
    assert ali.last_text.startswith(plain(texts.support))


@pytest.fixture
async def app_without_handle(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[App]:
    async for running in app_with(
        migrated_template, tmp_path, monkeypatch, support_handle="", support_hours_fa=""
    ):
        yield running


@smoke("۲.۷")
async def test_with_no_handle_configured_the_screen_says_nothing_rather_than_a_bare_at(
    app_without_handle: App,
) -> None:
    ali = app_without_handle.customer("ali")
    await ali.start()
    await ali.tap_reply(SUPPORT)
    assert "@" not in ali.last_text, ali.last_text
    assert "پشتیبانی:" not in ali.last_text


@smoke("۲.۸")
async def test_the_first_message_names_the_brand(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    assert "نئو نتورک" in ali.messages[0].text


@smoke("۲.۹")
async def test_the_main_menu_is_headed_by_the_brand(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(SUPPORT)
    await ali.tap("⬅️ بازگشت به منو")
    assert "نئو نتورک" in ali.last_text.splitlines()[0], ali.last_text
    assert ali.last_text == plain(texts.main_menu)


async def _reports(app: App) -> list[DisruptionReport]:
    return await app.all(select(DisruptionReport).order_by(DisruptionReport.id))


@smoke("۲.۱۰")
async def test_reattaching_the_same_group_adds_the_missing_disruption_topic_only(
    app: App,
) -> None:
    """A group connected before «📡 اختلال» existed: «🔗 تغییر گروه» with the same id adds it."""
    group = await app.connect_report_group(GROUP_CHAT_ID)
    async with app.session() as session:
        row = await session.get(ReportSettings, 1)
        before = dict(row.topics)
        # The state of a group set up before the topic existed.
        row.topics = {k: v for k, v in before.items() if k != "disruptions"}
        await session.commit()
    others = {k: v for k, v in before.items() if k != "disruptions"}

    admin = app.admin()
    await open_admin_panel(admin, "⚙️ تنظیمات")
    await tap_starting(admin, "📡 گروه گزارش")
    await tap_starting(admin, "🔗 تغییر گروه")
    mark = admin.mark()
    await admin.send(str(GROUP_CHAT_ID))

    async with app.session() as session:
        after = dict((await session.get(ReportSettings, 1)).topics)
    assert {k: v for k, v in after.items() if k != "disruptions"} == others, "old topics moved"
    assert after["disruptions"] not in before.values(), "a fresh topic, not an old one reused"
    assert len(after) == len(before)
    # The new topic got its test message; the old ones were not duplicated as new topics.
    tests = [m.text for m in await group.messages("disruptions") if "تست مسیر" in m.text]
    assert len(tests) == 1, tests
    summary = admin.texts_since(mark)[-1]
    assert "📡 اختلال" in summary and "آماده شد" in summary, summary


@smoke("۲.۱۱")
async def test_a_report_is_thanked_and_lands_in_the_disruption_topic_with_the_services(
    app: App,
) -> None:
    await publish_store(app)
    group = await app.connect_report_group()
    ali = app.customer("ali", first_name="Ali", username="ali_u")
    await ali.start()
    await ali.tap_reply("🎟 تست رایگان")
    await tap_starting(ali, "ویژه")
    mark = ali.mark()
    await report_disruption(ali, "ایرانسل", "تهران")
    assert plain(texts.disruption_thanks) in ali.texts_since(mark)
    (row,) = await _reports(app)
    assert (row.isp, row.city) == ("irancell", "تهران")
    await app.run_job("report_flush")
    real = [t for t in await group.texts("disruptions") if "تست مسیر" not in t]
    assert len(real) == 1, real
    assert "ایرانسل" in real[0] and "تهران" in real[0]
    assert "ویژه" in real[0], "the customer's service group should be named"
    assert "ali_u" in real[0] or "Ali" in real[0]


@smoke("۲.۱۲")
async def test_a_typed_city_that_is_a_phone_number_is_refused_then_a_real_one_is_saved(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await report_disruption(ali, "همراه اول", None)
    await ali.tap("✍️ شهر دیگه")
    assert ali.last_text == plain(texts.disruption_write_city)
    mark = ali.mark()
    await ali.send("0912")
    assert ali.texts_since(mark) == [plain(texts.disruption_bad_city)]
    assert await _reports(app) == []
    mark = ali.mark()
    await ali.send("رشت")
    assert ali.texts_since(mark) == [plain(texts.disruption_thanks)]
    (row,) = await _reports(app)
    assert (row.isp, row.city) == ("mci", "رشت")


@smoke("۲.۱۳")
async def test_a_second_report_within_half_an_hour_is_refused_and_not_stored(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    await report_disruption(ali, "ایرانسل", "تهران")
    await report_disruption(ali, "همراه اول", "مشهد")
    assert ali.last_text.startswith(plain(texts.disruption_too_soon))
    assert SUPPORT_HANDLE in ali.last_text, "a refused customer is pointed at a human"
    assert len(await _reports(app)) == 1
    # After the half hour it is accepted again.
    async with app.session() as session:
        for report in (await session.execute(select(DisruptionReport))).scalars():
            report.created_at = report.created_at - timedelta(minutes=31)
        await session.commit()
    mark = ali.mark()
    await report_disruption(ali, "همراه اول", "مشهد")
    assert plain(texts.disruption_thanks) in ali.texts_since(mark)
    assert len(await _reports(app)) == 2


@smoke("۲.۱۴")
async def test_the_next_days_digest_ranks_operators_with_their_top_city(app: App) -> None:
    group = await app.connect_report_group()
    people = []
    for index, (isp, city) in enumerate(
        [
            ("همراه اول", "تهران"),
            ("ایرانسل", "مشهد"),
            ("ایرانسل", "مشهد"),
            ("ایرانسل", "تهران"),
        ]
    ):
        person = app.customer(f"p{index}")
        await person.start()
        await report_disruption(person, isp, city)
        people.append(person)
    # The reports were made today; the digest at 00:05 summarises the day that ended.
    for report in await _reports(app):
        await app.age(DisruptionReport, report.id, days=1, columns=("created_at",))
    await app.run_job("digest")
    await app.run_job("report_flush")
    digest = [t for t in await group.texts("system") if "جمع‌بندی شب" in t]
    assert len(digest) == 1, digest
    lines = digest[0].splitlines()
    start = next(i for i, line in enumerate(lines) if "گزارش اختلال" in line)
    ranked = lines[start + 1 :]
    assert digits(ranked[0]).startswith("ایرانسل: 3"), ranked
    assert "بیشتر مشهد" in ranked[0]
    assert digits(ranked[1]).startswith("همراه اول: 1"), ranked
    assert "بیشتر تهران" in ranked[1]
