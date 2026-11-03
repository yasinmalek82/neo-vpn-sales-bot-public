"""Report group setup and the outbox.

The properties that matter: a report must never be able to roll back money, a
route is only «آماده» once a real message reached it, and no report may carry a
subscription link, an API key or a card number into a group.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models.report import (
    TOPIC_ICON_COLORS,
    TOPIC_ICON_COLORS_ALLOWED,
    TOPIC_KEYS,
    TOPIC_TITLES_FA,
    OutboxState,
    ReportOutbox,
)
from services.report_group import (
    ReportGroupError,
    attach_group,
    get_readiness,
    reconcile_topics,
    run_full_setup,
    set_enabled,
    set_topic,
    verify_topic,
)
from services.report_outbox import MAX_ATTEMPTS, enqueue_report, flush_reports, redact

CHAT_ID = -1001234567890


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class FakeBot:
    """Records sends; can be told to be a non-forum group, or to fail."""

    def __init__(
        self,
        *,
        chat_type: str = "supergroup",
        is_forum: bool = True,
        member_status: str = "administrator",
        reachable: bool = True,
        can_manage_topics: bool = True,
    ) -> None:
        self.chat_type = chat_type
        self.is_forum = is_forum
        self.member_status = member_status
        self.reachable = reachable
        self.can_manage_topics = can_manage_topics
        self.sent: list[dict] = []
        self.created_topics: list[dict] = []
        self.edited_topics: list[dict] = []
        self.fail_sends = False
        self.fail_topic_creation: set[str] = set()
        self.missing_threads: set[int] = set()
        self.flaky_threads: set[int] = set()
        self.topic_names: dict[int, str] = {}
        self._next_thread_id = 100

    async def get_chat(self, chat_id: int):  # noqa: ANN201
        if not self.reachable:
            raise RuntimeError("chat not found")
        return SimpleNamespace(
            id=chat_id, type=self.chat_type, is_forum=self.is_forum, title="گزارش‌ها"
        )

    async def get_me(self):  # noqa: ANN201
        return SimpleNamespace(id=777, username="neo_bot")

    async def get_chat_member(self, chat_id: int, user_id: int):  # noqa: ANN201, ARG002
        return SimpleNamespace(status=self.member_status, can_manage_topics=self.can_manage_topics)

    async def create_forum_topic(
        self, *, chat_id: int, name: str, icon_color=None
    ):  # noqa: ANN001, ANN201
        if name in self.fail_topic_creation:
            raise RuntimeError("topic creation refused")
        self._next_thread_id += 1
        self.topic_names[self._next_thread_id] = name
        self.created_topics.append({"chat_id": chat_id, "name": name, "icon_color": icon_color})
        return SimpleNamespace(message_thread_id=self._next_thread_id, name=name)

    async def edit_forum_topic(
        self, *, chat_id: int, message_thread_id: int, name: str
    ):  # noqa: ANN201
        # The three answers are Telegram's own, read off the live Bot API on
        # 2026-10-03: a deleted thread, a rename to the name it already has,
        # and anything else.
        if message_thread_id in self.missing_threads:
            raise RuntimeError("Telegram server says - Bad Request: TOPIC_ID_INVALID")
        if message_thread_id in self.flaky_threads:
            raise RuntimeError("Telegram server says - Bad Gateway")
        if self.topic_names.get(message_thread_id) == name:
            raise RuntimeError("Telegram server says - Bad Request: TOPIC_NOT_MODIFIED")
        self.topic_names[message_thread_id] = name
        self.edited_topics.append(
            {"chat_id": chat_id, "message_thread_id": message_thread_id, "name": name}
        )
        return True

    async def send_message(
        self, *, chat_id: int, text: str, message_thread_id=None, reply_markup=None
    ):  # noqa: ANN001, ANN201
        if self.fail_sends:
            raise RuntimeError("no permission")
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "message_thread_id": message_thread_id,
                "reply_markup": reply_markup,
            }
        )
        return SimpleNamespace(message_id=len(self.sent))


async def _ready_group(session: AsyncSession, bot: FakeBot) -> None:
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await set_topic(session, topic_key="sales", thread_id=11)
    await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]
    await set_enabled(session, enabled=True)
    await session.commit()


# --------------------------------------------------------------------------- #
# Attaching a group
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_attach_records_the_group(session: AsyncSession) -> None:
    bot = FakeBot()
    readiness = await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    assert readiness.attached
    assert readiness.chat_id == CHAT_ID
    assert [t.key for t in readiness.topics] == list(TOPIC_KEYS)


class _FullIdOnlyBot(FakeBot):
    """Bot API knows a supergroup only by its ``-100…`` id."""

    async def get_chat(self, chat_id: int):  # noqa: ANN201
        if not str(chat_id).startswith("-100"):
            raise RuntimeError("Bad Request: chat not found")
        return await super().get_chat(chat_id)


@pytest.mark.asyncio
async def test_a_short_group_id_is_tried_in_its_full_form(session: AsyncSession) -> None:
    """The owner pasted «-4336742277» (Telegram Web's form) and was told
    the bot had no access to a group it administered."""
    bot = _FullIdOnlyBot()
    for typed in (-4336742277, 4336742277):
        readiness = await attach_group(session, bot, chat_id=typed)  # type: ignore[arg-type]
        assert readiness.chat_id == -1004336742277


def test_copied_ids_lose_bidi_marks_and_unicode_minus() -> None:
    from bot.handlers.admin import _parse_signed_toman

    assert _parse_signed_toman("\u200e-1004336742277") == -1004336742277
    assert _parse_signed_toman("\u22121004336742277") == -1004336742277


@pytest.mark.asyncio
async def test_each_setup_mistake_names_its_own_fix(session: AsyncSession) -> None:
    """A generic «خطا» would leave the owner with nothing to do."""
    cases = [
        (FakeBot(reachable=False), "عضو"),  # bot not in the group
        (FakeBot(is_forum=False), "Topics"),  # forum mode off
        (FakeBot(member_status="member"), "ادمین"),  # not an admin
        (FakeBot(can_manage_topics=False), "Manage Topics"),  # cannot create topics
        (FakeBot(chat_type="private"), "گروه"),  # not a group
    ]
    for bot, expected in cases:
        with pytest.raises(ReportGroupError) as exc:
            await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
        assert expected in str(exc.value), (expected, str(exc.value))


@pytest.mark.asyncio
async def test_switching_groups_clears_stale_thread_ids(session: AsyncSession) -> None:
    """Thread ids are per-group; keeping them would route reports into nothing."""
    bot = FakeBot()
    await _ready_group(session, bot)

    readiness = await attach_group(session, bot, chat_id=-1009999999999)  # type: ignore[arg-type]
    await session.commit()
    assert all(not t.configured for t in readiness.topics)


# --------------------------------------------------------------------------- #
# Proving a route
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_topic_is_ready_only_after_a_real_send(session: AsyncSession) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    readiness = await set_topic(session, topic_key="sales", thread_id=11)
    sales = next(t for t in readiness.topics if t.key == "sales")
    assert sales.configured and not sales.ready, "configured is not proof"

    readiness = await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]
    await session.commit()
    sales = next(t for t in readiness.topics if t.key == "sales")
    assert sales.ready
    assert bot.sent[-1]["message_thread_id"] == 11


@pytest.mark.asyncio
async def test_a_failed_test_leaves_the_topic_unproven(session: AsyncSession) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await set_topic(session, topic_key="sales", thread_id=11)
    bot.fail_sends = True

    with pytest.raises(ReportGroupError):
        await verify_topic(session, bot, topic_key="sales")  # type: ignore[arg-type]
    readiness = await get_readiness(session)
    assert not next(t for t in readiness.topics if t.key == "sales").ready


@pytest.mark.asyncio
async def test_repointing_a_topic_clears_its_proof(session: AsyncSession) -> None:
    bot = FakeBot()
    await _ready_group(session, bot)
    readiness = await set_topic(session, topic_key="sales", thread_id=22)
    assert not next(t for t in readiness.topics if t.key == "sales").ready


@pytest.mark.asyncio
async def test_reporting_cannot_be_enabled_before_anything_is_proven(
    session: AsyncSession,
) -> None:
    """Otherwise the owner believes reports flow while nothing arrives."""
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await set_topic(session, topic_key="sales", thread_id=11)
    with pytest.raises(ReportGroupError):
        await set_enabled(session, enabled=True)


# --------------------------------------------------------------------------- #
# The outbox
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_enqueue_writes_a_row_and_sends_nothing(session: AsyncSession) -> None:
    """Enqueue runs inside a money transaction, so it must not touch Telegram."""
    bot = FakeBot()
    await _ready_group(session, bot)
    before = len(bot.sent)

    await enqueue_report(session, event_key="order:1:active", topic_key="sales", body="فروش")
    await session.commit()

    assert len(bot.sent) == before
    rows = (await session.execute(select(ReportOutbox))).scalars().all()
    assert len(rows) == 1 and rows[0].state is OutboxState.pending


@pytest.mark.asyncio
async def test_a_repeated_event_is_queued_once(session: AsyncSession) -> None:
    bot = FakeBot()
    await _ready_group(session, bot)

    first = await enqueue_report(session, event_key="order:1:active", topic_key="sales", body="یک")
    second = await enqueue_report(session, event_key="order:1:active", topic_key="sales", body="دو")
    await session.commit()

    assert first is not None and second is None
    assert len((await session.execute(select(ReportOutbox))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_flush_delivers_to_the_right_topic(session: AsyncSession) -> None:
    bot = FakeBot()
    await _ready_group(session, bot)
    await enqueue_report(session, event_key="e1", topic_key="sales", body="فروش جدید")
    await session.commit()

    sent, failed = await flush_reports(session, bot)  # type: ignore[arg-type]
    assert (sent, failed) == (1, 0)
    assert bot.sent[-1]["message_thread_id"] == 11
    assert bot.sent[-1]["chat_id"] == CHAT_ID
    assert bot.sent[-1]["reply_markup"] is None


@pytest.mark.asyncio
async def test_a_linked_report_carries_the_customer_card_button(session: AsyncSession) -> None:
    """The button opens the private chat. It must not be a callback that edits the report."""
    bot = FakeBot()
    await _ready_group(session, bot)
    await enqueue_report(
        session, event_key="e-link", topic_key="sales", body="فروش", link_user_id=7
    )
    await session.commit()

    await flush_reports(session, bot)  # type: ignore[arg-type]
    button = bot.sent[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "کارت مشتری"
    assert button.url == "https://t.me/neo_bot?start=admcu_7"
    assert button.callback_data is None


@pytest.mark.asyncio
async def test_flush_does_nothing_while_reporting_is_off(
    session: AsyncSession,
) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await enqueue_report(session, event_key="e1", topic_key="sales", body="x")
    await session.commit()

    assert await flush_reports(session, bot) == (0, 0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failing_send_retries_then_gives_up(session: AsyncSession) -> None:
    """A permanently broken topic must not keep a growing queue alive."""
    bot = FakeBot()
    await _ready_group(session, bot)
    await enqueue_report(session, event_key="e1", topic_key="sales", body="x")
    await session.commit()
    bot.fail_sends = True

    for _ in range(MAX_ATTEMPTS):
        await flush_reports(session, bot)  # type: ignore[arg-type]

    row = (await session.execute(select(ReportOutbox))).scalars().one()
    assert row.state is OutboxState.failed
    assert row.attempts == MAX_ATTEMPTS
    assert row.last_error


@pytest.mark.asyncio
async def test_a_sent_report_is_not_sent_again(session: AsyncSession) -> None:
    bot = FakeBot()
    await _ready_group(session, bot)
    await enqueue_report(session, event_key="e1", topic_key="sales", body="x")
    await session.commit()

    await flush_reports(session, bot)  # type: ignore[arg-type]
    count = len(bot.sent)
    await flush_reports(session, bot)  # type: ignore[arg-type]
    assert len(bot.sent) == count


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redaction_strips_what_must_never_reach_a_group() -> None:
    assert "https://" not in redact("لینک: https://panel.example/sub/abc123")
    assert "pg_key_" not in redact("کلید pg_key_abcdef123456")
    assert "6037991234567890" not in redact("کارت 6037991234567890")
    assert "6037-9912-3456-7890" not in redact("کارت 6037-9912-3456-7890")
    assert "AAH" not in redact("توکن 8949673340:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def test_redaction_keeps_the_readable_parts() -> None:
    body = "سفارش ۱۲ فعال شد. مبلغ: ۱۵۰٬۰۰۰ تومان"
    assert redact(body) == body


@pytest.mark.asyncio
async def test_a_queued_report_is_redacted_at_write_time(
    session: AsyncSession,
) -> None:
    """Redacting on the way in means no delivery path can leak by forgetting."""
    bot = FakeBot()
    await _ready_group(session, bot)
    await enqueue_report(
        session,
        event_key="e1",
        topic_key="sales",
        body="سرویس آماده شد: https://panel.example/sub/secret",
    )
    await session.commit()

    row = (await session.execute(select(ReportOutbox))).scalars().one()
    assert "panel.example" not in row.body


# --------------------------------------------------------------------------- #
# Automatic topic creation
#
# Asking the owner to copy five thread ids out of «Copy Link» was five chances
# to paste the wrong one — and a wrong id is indistinguishable from a right one
# until a real report goes missing.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_bot_creates_every_topic_itself(session: AsyncSession) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    created, reused, failures = await reconcile_topics(session, bot)  # type: ignore[arg-type]
    await session.commit()

    assert set(created) == set(TOPIC_KEYS)
    assert reused == [] and failures == []
    readiness = await get_readiness(session)
    assert all(t.configured for t in readiness.topics)
    # Distinct threads, or two kinds of report would share one topic.
    ids = [t.thread_id for t in readiness.topics]
    assert len(set(ids)) == len(ids)


@pytest.mark.asyncio
async def test_created_topics_carry_their_persian_names_and_colours(
    session: AsyncSession,
) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await reconcile_topics(session, bot)  # type: ignore[arg-type]

    names = {t["name"] for t in bot.created_topics}
    assert names == set(TOPIC_TITLES_FA.values())
    colours = {t["icon_color"] for t in bot.created_topics}
    assert colours == set(TOPIC_ICON_COLORS.values())
    assert set(TOPIC_ICON_COLORS.values()) <= TOPIC_ICON_COLORS_ALLOWED


@pytest.mark.asyncio
async def test_running_setup_twice_renames_instead_of_duplicating(
    session: AsyncSession,
) -> None:
    bot = FakeBot()
    await attach_group(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await reconcile_topics(session, bot)  # type: ignore[arg-type]
    created, adopted, _ = await reconcile_topics(session, bot)  # type: ignore[arg-type]
    await session.commit()

    assert created == []
    assert set(adopted) == set(TOPIC_KEYS)
    assert len(bot.created_topics) == len(TOPIC_KEYS), "no second set of topics"
    # Nothing needed renaming: Telegram answered TOPIC_NOT_MODIFIED to each, and
    # that answer is the proof the thread is still there.
    assert bot.edited_topics == []


@pytest.mark.asyncio
async def test_a_renamed_topic_is_renamed_back(session: AsyncSession) -> None:
    """Someone editing «🛒 فروش» to something else does not get a duplicate."""
    bot = FakeBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    before = len(bot.created_topics)
    sales = next(t for t in (await get_readiness(session)).topics if t.key == "sales")
    assert sales.thread_id is not None
    bot.topic_names[sales.thread_id] = "اسم دیگر"

    await reconcile_topics(session, bot)  # type: ignore[arg-type]
    await session.commit()

    assert len(bot.created_topics) == before
    assert [e["name"] for e in bot.edited_topics] == [TOPIC_TITLES_FA["sales"]]


@pytest.mark.asyncio
async def test_a_rename_that_fails_for_another_reason_does_not_make_a_second_topic(
    session: AsyncSession,
) -> None:
    """A server error on the rename is not proof the topic was deleted."""
    bot = FakeBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    before = len(bot.created_topics)
    wallet = next(t for t in (await get_readiness(session)).topics if t.key == "wallet")
    assert wallet.thread_id is not None
    bot.flaky_threads = {wallet.thread_id}

    created, adopted, failures = await reconcile_topics(session, bot)  # type: ignore[arg-type]
    await session.commit()

    assert created == [] and len(bot.created_topics) == before
    assert "wallet" not in adopted
    assert [key for key, _ in failures] == ["wallet"]
    kept = next(t for t in (await get_readiness(session)).topics if t.key == "wallet")
    assert kept.thread_id == wallet.thread_id, "the saved thread stays where it was"


@pytest.mark.asyncio
async def test_a_topic_deleted_in_telegram_is_rebuilt(session: AsyncSession) -> None:
    bot = FakeBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    readiness = await get_readiness(session)
    lost = next(t for t in readiness.topics if t.key == "errors")
    assert lost.thread_id is not None
    bot.missing_threads = {lost.thread_id}

    created, adopted, failures = await reconcile_topics(session, bot)  # type: ignore[arg-type]
    await session.commit()

    assert created == ["errors"]
    assert "errors" not in adopted
    assert failures == []
    fresh = next(t for t in (await get_readiness(session)).topics if t.key == "errors")
    assert fresh.thread_id != lost.thread_id
    # A rebuilt route has lost its proof and must be tested again.
    assert not fresh.ready


@pytest.mark.asyncio
async def test_one_message_attaches_creates_proves_and_switches_on(
    session: AsyncSession,
) -> None:
    """The whole point: the owner sends a chat id and nothing else."""
    bot = FakeBot()
    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()

    assert result.ok
    assert set(result.created) == set(TOPIC_KEYS)
    assert set(result.verified) == set(TOPIC_KEYS)
    assert result.readiness.enabled
    assert result.readiness.all_ready


@pytest.mark.asyncio
async def test_a_partial_setup_reports_exactly_what_failed(
    session: AsyncSession,
) -> None:
    """A group where four of five topics worked must not read as success."""
    bot = FakeBot()
    bot.fail_topic_creation = {TOPIC_TITLES_FA["system"]}

    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()

    assert not result.ok
    assert "system" not in result.verified
    assert [key for key, _ in result.failures] == ["system"]
    # The four that worked are still live rather than being rolled back.
    assert result.readiness.enabled
    assert result.readiness.ready_count == len(TOPIC_KEYS) - 1


@pytest.mark.asyncio
async def test_rerunning_setup_repairs_the_missing_topic(
    session: AsyncSession,
) -> None:
    bot = FakeBot()
    bot.fail_topic_creation = {TOPIC_TITLES_FA["system"]}
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()

    bot.fail_topic_creation = set()
    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()

    assert result.ok
    assert result.created == ["system"]
    assert result.readiness.all_ready


@pytest.mark.asyncio
async def test_setup_on_a_fresh_group_starts_from_scratch(
    session: AsyncSession,
) -> None:
    """Thread ids belong to a group; moving groups must not reuse them."""
    bot = FakeBot()
    await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    first_ids = {t.key: t.thread_id for t in (await get_readiness(session)).topics}

    result = await run_full_setup(session, bot, chat_id=-1009999999999)  # type: ignore[arg-type]
    await session.commit()

    second_ids = {t.key: t.thread_id for t in result.readiness.topics}
    assert all(first_ids[k] != second_ids[k] for k in TOPIC_KEYS)


@pytest.mark.asyncio
async def test_a_successful_setup_requeues_recent_failed_reports(
    session: AsyncSession,
) -> None:
    """Reports that burned their attempts while the route was broken are not lost."""
    from datetime import UTC, datetime, timedelta

    from database.models.report import OutboxState, ReportOutbox

    old = datetime.now(UTC) - timedelta(days=9)
    for key, state, created in (
        ("r:recent", OutboxState.failed, None),
        ("r:old", OutboxState.failed, old),
        ("r:sent", OutboxState.sent, None),
    ):
        row = ReportOutbox(
            event_key=key, topic_key="sales", body="b", state=state, attempts=MAX_ATTEMPTS
        )
        if created is not None:
            row.created_at = created
        session.add(row)
    await session.commit()

    bot = FakeBot()
    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()

    assert result.ok and result.requeued == 1
    rows = {r.event_key: r for r in (await session.scalars(select(ReportOutbox))).all()}
    assert rows["r:recent"].state is OutboxState.pending and rows["r:recent"].attempts == 0
    assert rows["r:old"].state is OutboxState.failed
    assert rows["r:sent"].state is OutboxState.sent

    from bot.handlers.admin import _setup_report

    assert "۱" in _setup_report(result) and "دوباره در صف" in _setup_report(result)


@pytest.mark.asyncio
async def test_a_partial_setup_leaves_failed_reports_alone(session: AsyncSession) -> None:
    from database.models.report import OutboxState, ReportOutbox

    session.add(
        ReportOutbox(
            event_key="r:x", topic_key="sales", body="b", state=OutboxState.failed, attempts=5
        )
    )
    await session.commit()
    bot = FakeBot()
    bot.fail_topic_creation = {TOPIC_TITLES_FA["system"]}
    result = await run_full_setup(session, bot, chat_id=CHAT_ID)  # type: ignore[arg-type]
    await session.commit()
    assert not result.ok and result.requeued == 0
    row = await session.scalar(select(ReportOutbox))
    assert row is not None and row.state is OutboxState.failed
