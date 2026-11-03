"""Live finding: the bot talked inside groups.

Added to a new group, it answered the service message with its private-chat
fallback («این پیام رو نفهمیدم…»), again when the group became a forum, and
once inside each of the 8 report topics it had just created. Its customer and
admin UI is for private chats; in a group it only posts reports.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.report_group import run_full_setup
from tests.e2e._live_helpers import bot_calls, press_in_group, say_in_group
from tests.e2e.harness import App

BOT_AS_MEMBER = {"id": 777_000_111, "is_bot": True, "first_name": "Neo Test"}


@pytest.mark.parametrize(
    "fields",
    [
        {"new_chat_members": [BOT_AS_MEMBER]},
        {"group_chat_created": True},
        {"text": "سلام"},
        {"text": "/start"},
        {"text": "/admin"},
        {"text": "/cancel"},
        {"text": "🛒 خرید اشتراک"},
    ],
    ids=["bot-added", "group-created", "plain-text", "start", "admin", "cancel", "menu-label"],
)
async def test_bot_says_nothing_in_a_group(app: App, fields: dict[str, Any]) -> None:
    owner = app.admin()
    await owner.start()
    group = app.tg.add_forum(-1009990001, is_forum=False)
    before = bot_calls(app)
    owner_mark = owner.mark()

    await say_in_group(owner, group, **fields)

    assert [m for m in group.messages.values() if m.from_bot] == []
    assert owner.messages_since(owner_mark) == []
    assert bot_calls(app) == before


async def test_bot_says_nothing_inside_the_report_topics_it_created(app: App) -> None:
    owner = app.admin()
    await owner.start()
    report_group = await app.connect_report_group()
    chat = report_group.chat
    threads = app.tg.topic_threads(chat.id)
    assert len(threads) == 8
    sent_by_bot = [m.message_id for m in chat.messages.values() if m.from_bot]
    before = bot_calls(app)

    # Telegram delivers one «topic created» service message per topic.
    for name, thread_id in threads.items():
        await say_in_group(
            owner,
            chat,
            thread_id=thread_id,
            forum_topic_created={"name": str(name), "icon_color": 7322096},
        )
    await say_in_group(owner, chat, thread_id=next(iter(threads.values())), text="این چیه؟")

    assert [m.message_id for m in chat.messages.values() if m.from_bot] == sent_by_bot
    assert bot_calls(app) == before


async def test_a_callback_from_a_group_opens_no_private_screen(app: App) -> None:
    owner = app.admin()
    await owner.start()
    report_group = await app.connect_report_group()
    target = (await report_group.messages("sales"))[0]
    before = bot_calls(app)
    owner_mark = owner.mark()

    await press_in_group(owner, target, "admin:home")

    assert target.edits == 0
    assert owner.messages_since(owner_mark) == []
    assert bot_calls(app) == before


async def test_private_ui_and_report_group_still_work_after_group_chatter(app: App) -> None:
    owner = app.admin()
    await owner.start()
    report_group = await app.connect_report_group()
    await say_in_group(owner, report_group.chat, text="سلام")
    tests_before = len(await report_group.messages("sales"))

    await owner.send("/admin")
    assert owner.buttons  # the private admin home still opens

    async with app.session() as session:  # the real one-tap setup, run again
        result = await run_full_setup(session, app.bot, chat_id=report_group.chat.id)
        await session.commit()
    assert result.ok
    assert len(await report_group.messages("sales")) == tests_before + 1
