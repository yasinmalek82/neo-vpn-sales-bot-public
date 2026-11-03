"""Live finding: attaching the report group hit Telegram's flood control.

Eight topics and eight test messages went out back to back; the last three
failed with «Flood control exceeded … Retry after N», the group read
«نیمه‌آماده», and the admin screen showed that English text cut mid-word.
"""

from __future__ import annotations

import re

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin import _setup_report
from database.models.report import TOPIC_KEYS
from services import report_group
from services.report_group import ReportGroupError, run_full_setup, verify_topic
from tests.unit.test_report_group import CHAT_ID, FakeBot, session  # noqa: F401

_METHOD = SendMessage(chat_id=CHAT_ID, text="x")
LATIN_WORD = re.compile(r"[A-Za-z]{3,}")


def _flood(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=_METHOD, message="Too Many Requests", retry_after=seconds)


class ScriptedBot(FakeBot):
    """A FakeBot whose n-th send (1-based) raises what the script says."""

    def __init__(self, script: dict[int, Exception] | None = None) -> None:
        super().__init__()
        self.script = dict(script or {})
        self.send_attempts = 0
        self.timeline: list[str] = []

    async def send_message(self, **kwargs):  # noqa: ANN003, ANN201
        self.send_attempts += 1
        self.timeline.append("send")
        exc = self.script.get(self.send_attempts)
        if exc is not None:
            raise exc
        return await super().send_message(**kwargs)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(report_group.asyncio, "sleep", fake_sleep)
    return recorded


async def test_a_flood_wait_is_honoured_and_the_send_retried_once(
    session: AsyncSession, sleeps: list[float]  # noqa: F811
) -> None:
    bot = ScriptedBot({6: _flood(4)})

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    assert result.ok, result.failures
    assert set(result.verified) == set(TOPIC_KEYS)
    assert bot.send_attempts == len(TOPIC_KEYS) + 1
    assert max(sleeps) >= 4, "the bot must wait at least what Telegram asked for"


async def test_a_flood_that_survives_the_retry_fails_that_topic_in_persian(
    session: AsyncSession, sleeps: list[float]  # noqa: F811, ARG001
) -> None:
    bot = ScriptedBot({6: _flood(4), 7: _flood(4)})

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    assert not result.ok
    assert [key for key, _ in result.failures] == [TOPIC_KEYS[5]]
    assert result.readiness.ready_count == len(TOPIC_KEYS) - 1
    # one retry, not a loop: 8 topics + the single retry
    assert bot.send_attempts == len(TOPIC_KEYS) + 1
    screen = _setup_report(result)
    assert "نیمه‌آماده" in screen
    assert "🧪 تست" in screen
    assert not LATIN_WORD.search(re.sub(r"<[^>]+>", "", screen)), screen


async def test_a_very_long_flood_wait_is_not_slept_through(
    session: AsyncSession, sleeps: list[float]  # noqa: F811
) -> None:
    bot = ScriptedBot({1: _flood(3600)})

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    assert [key for key, _ in result.failures] == [TOPIC_KEYS[0]]
    assert sum(sleeps) < 120
    assert max(sleeps, default=0) < 3600


async def test_test_messages_are_paced(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    bot = ScriptedBot()

    async def fake_sleep(seconds: float) -> None:
        bot.timeline.append(f"sleep {seconds}")

    monkeypatch.setattr(report_group.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(report_group, "SEND_GAP_SECONDS", 1.5)

    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    sends = [i for i, step in enumerate(bot.timeline) if step == "send"]
    assert len(sends) == len(TOPIC_KEYS)
    for a, b in zip(sends, sends[1:], strict=False):
        assert "sleep 1.5" in bot.timeline[a:b], "two test messages went out back to back"


async def test_a_flood_on_renaming_a_topic_does_not_duplicate_it(
    session: AsyncSession, sleeps: list[float]  # noqa: F811, ARG001
) -> None:
    bot = ScriptedBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    created_first = len(bot.created_topics)
    original_edit = bot.edit_forum_topic

    async def flooded_edit(**kwargs):  # noqa: ANN003, ANN202
        if kwargs["name"] == bot.created_topics[2]["name"]:
            raise _flood(3600)
        return await original_edit(**kwargs)

    bot.edit_forum_topic = flooded_edit  # type: ignore[method-assign]

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    assert len(bot.created_topics) == created_first, "a throttled rename is not a deleted topic"
    assert [key for key, _ in result.failures] == [TOPIC_KEYS[2]]


@pytest.mark.parametrize(
    ("exc", "must_say"),
    [
        (_flood(30), "🧪 تست"),
        (TelegramForbiddenError(method=_METHOD, message="Forbidden: bot was kicked"), "ادمین"),
        (
            TelegramBadRequest(
                method=_METHOD, message="Bad Request: not enough rights to send text messages"
            ),
            "ادمین",
        ),
        (
            TelegramBadRequest(method=_METHOD, message="Bad Request: message thread not found"),
            "تاپیک",
        ),
        (TelegramBadRequest(method=_METHOD, message="Bad Request: TOPIC_CLOSED"), "تاپیک"),
        (RuntimeError("boom at 0x7f"), "نامشخص"),
    ],
    ids=["flood", "kicked", "no-rights", "thread-gone", "topic-closed", "unknown"],
)
async def test_the_single_topic_test_names_the_reason_in_persian(
    session: AsyncSession, sleeps: list[float], exc: Exception, must_say: str  # noqa: F811
) -> None:
    bot = ScriptedBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    del sleeps[:]
    bot.script = {bot.send_attempts + 1: exc}

    with pytest.raises(ReportGroupError) as caught:
        await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]

    said = str(caught.value)
    assert must_say in said
    assert not LATIN_WORD.search(said), said
    assert len(said) <= 200, "it is shown in a callback alert"
    # A tap must be answered now: the «🧪 تست» button never sleeps on a flood.
    assert [s for s in sleeps if s > 2] == []


async def test_a_topic_that_cannot_be_created_is_explained_in_persian(
    session: AsyncSession, sleeps: list[float]  # noqa: F811, ARG001
) -> None:
    bot = ScriptedBot()
    original_create = bot.create_forum_topic

    async def refused_create(**kwargs):  # noqa: ANN003, ANN202
        if len(bot.created_topics) == 3:
            raise TelegramBadRequest(
                method=_METHOD, message="Bad Request: not enough rights to manage topics"
            )
        return await original_create(**kwargs)

    bot.create_forum_topic = refused_create  # type: ignore[method-assign]

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]

    assert result.failures
    screen = re.sub(r"<[^>]+>", "", _setup_report(result))
    assert "ادمین" in screen
    assert not LATIN_WORD.search(screen), screen
