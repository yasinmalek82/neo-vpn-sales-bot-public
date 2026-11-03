"""«📡 گزارش اختلال»: ISP and city, counted so the worst ISP is fixed first.

The owner's decision (2026-09-26): from support, the customer picks the ISP,
then the city (a button for the big ones, typed otherwise); the bot adds which
services they have; each report lands in the report group, and the nightly
digest ranks ISPs by count.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import disruption as handlers
from bot.keyboards.disruption import DisruptionCB, support_keyboard
from bot.states import DisruptionStates
from bot.texts import texts
from database.base import Base
from database.models import DisruptionReport, Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.report import (
    TOPIC_DISRUPTIONS,
    TOPIC_ICON_COLORS,
    TOPIC_ICON_COLORS_ALLOWED,
    TOPIC_KEYS,
    ReportOutbox,
)
from services.disruption import (
    CITIES,
    COOLDOWN,
    DisruptionError,
    disruption_counts,
    normalize_city,
    record_disruption,
)
from services.nightly_digest import build_nightly_digest, previous_tehran_day
from services.store_admin import commit_and_activate, create_draft_category, set_category_trial

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, tg: int = 600) -> User:
    user = User(
        telegram_id=tg,
        username=f"c{tg}",
        first_name="C",
        language_code="fa",
        referral_code=f"D{tg:07d}",
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return user


async def _reports(session: AsyncSession) -> list[DisruptionReport]:
    return list((await session.execute(select(DisruptionReport))).scalars().all())


# --------------------------------------------------------------------------- #
# The city is a city
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("تهران", "تهران"),
        ("  بندر   عباس ", "بندر عباس"),
        ("كرمانشاه", "کرمانشاه"),  # Arabic kaf
        ("خرم‌آباد", "خرم‌آباد"),  # ZWNJ kept
        ("Rasht", "Rasht"),
        ("ا", None),
        ("09123456789", None),
        ("https://x.y", None),
        ("@neo", None),
        ("", None),
        ("ت" * 41, None),
    ],
)
def test_a_typed_city_is_cleaned_or_refused(raw: str, expected: str | None) -> None:
    assert normalize_city(raw) == expected


# --------------------------------------------------------------------------- #
# Storing one report
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_report_stores_the_isp_key_and_goes_to_its_topic(session: AsyncSession) -> None:
    user = await _user(session)
    await record_disruption(session, user=user, isp="mci", city="  تهران ", now=NOW)

    [row] = await _reports(session)
    assert (row.isp, row.city) == ("mci", "تهران")
    outbox = (await session.execute(select(ReportOutbox))).scalars().all()
    assert [o.topic_key for o in outbox] == [TOPIC_DISRUPTIONS]
    assert outbox[0].event_key == f"disruption:{row.id}"
    assert "همراه اول" in outbox[0].body and "تهران" in outbox[0].body


@pytest.mark.asyncio
async def test_a_report_names_the_reporters_live_services(session: AsyncSession) -> None:
    """Direct or tunnel is told by the group of each live service, trials included."""
    direct = await create_draft_category(session, title_fa="نامحدود اقتصادی", kind="unlimited")
    tunnel = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await set_category_trial(session, tunnel.id, data_limit_gb=1, duration_days=1)
    from services.store_admin import create_draft_package

    pkg = await create_draft_package(
        session,
        category_id=direct.id,
        title_fa="۱ کاربره",
        fixed_price_toman=150_000,
        concurrent_users=1,
        duration_months=1,
    )
    old_group = await create_draft_category(session, title_fa="نامحدود قدیمی", kind="unlimited")
    old_pkg = await create_draft_package(
        session,
        category_id=old_group.id,
        title_fa="قدیمی",
        fixed_price_toman=100_000,
        concurrent_users=1,
        duration_months=1,
    )
    await commit_and_activate(session, notes="seed")
    user = await _user(session)
    plan = Plan(
        name="p",
        description="p",
        data_limit_gb=0,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    for kwargs in (
        {"store_package_slug": str(pkg.slug)},
        {"trial_kind": "personal", "trial_category_slug": str(tunnel.slug)},
        # Not live, and its own group: must not be named.
        {"store_package_slug": str(old_pkg.slug), "status": OrderStatus.EXPIRED},
    ):
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=kwargs.pop("status", OrderStatus.ACTIVE),
                price_toman=0,
                discount_toman=0,
                **kwargs,
            )
        )
    await session.flush()

    row = await record_disruption(session, user=user, isp="irancell", city="قم", now=NOW)
    assert row.service_groups == "نامحدود اقتصادی، حجمی ویژه"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("isp", "city", "code"),
    [("mtn", "تهران", "unknown_isp"), ("mci", "0912", "bad_city")],
)
async def test_a_bad_report_stores_nothing(
    session: AsyncSession, isp: str, city: str, code: str
) -> None:
    user = await _user(session)
    with pytest.raises(DisruptionError, match=code):
        await record_disruption(session, user=user, isp=isp, city=city, now=NOW)
    assert await _reports(session) == []


@pytest.mark.asyncio
async def test_repeated_taps_cannot_outvote_an_isp(session: AsyncSession) -> None:
    user = await _user(session)
    await record_disruption(session, user=user, isp="mci", city="تهران", now=NOW)
    with pytest.raises(DisruptionError, match="too_soon"):
        await record_disruption(
            session, user=user, isp="shatel", city="تهران", now=NOW + COOLDOWN / 2
        )
    await record_disruption(
        session, user=user, isp="mci", city="تهران", now=NOW + COOLDOWN + timedelta(seconds=1)
    )
    assert len(await _reports(session)) == 2


# --------------------------------------------------------------------------- #
# Counting and the nightly digest
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_isps_are_ranked_by_reports_with_their_worst_city(session: AsyncSession) -> None:
    rows = [
        ("mci", "تهران"),
        ("mci", "تهران"),
        ("mci", "کرج"),
        ("irancell", "مشهد"),
        ("shatel", "شیراز"),
        ("shatel", "تبریز"),
    ]
    for index, (isp, city) in enumerate(rows):
        user = await _user(session, 700 + index)
        await record_disruption(session, user=user, isp=isp, city=city, now=NOW)
    outside = await _user(session, 799)
    await record_disruption(
        session, user=outside, isp="irancell", city="قم", now=NOW - timedelta(days=2)
    )

    counts = await disruption_counts(
        session, start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1)
    )
    assert [(c.isp, c.count, c.top_city) for c in counts] == [
        ("mci", 3, "تهران"),
        ("shatel", 2, "تبریز"),  # a tie goes to one city, the same one every time
        ("irancell", 1, "مشهد"),
    ]


@pytest.mark.asyncio
async def test_the_nightly_digest_ranks_yesterdays_reports(session: AsyncSession) -> None:
    run_at = datetime(2026, 9, 27, 0, 5, tzinfo=UTC)
    start, _end, _key, _label = previous_tehran_day(run_at)
    for index in range(2):
        user = await _user(session, 810 + index)
        await record_disruption(
            session, user=user, isp="tci", city="اهواز", now=start + timedelta(hours=3)
        )
    report = await build_nightly_digest(session, now=run_at)
    assert "📡 گزارش اختلال" in report.body
    assert "مخابرات: ۲ • بیشتر اهواز" in report.body


@pytest.mark.asyncio
async def test_a_quiet_day_has_no_disruption_section(session: AsyncSession) -> None:
    report = await build_nightly_digest(session, now=datetime(2026, 9, 27, 0, 5, tzinfo=UTC))
    assert "اختلال" not in report.body


def test_the_topic_exists_with_an_allowed_colour() -> None:
    """createForumTopic rejects any other colour, and a missing key has no topic."""
    assert TOPIC_DISRUPTIONS in TOPIC_KEYS
    assert TOPIC_ICON_COLORS[TOPIC_DISRUPTIONS] in TOPIC_ICON_COLORS_ALLOWED


# --------------------------------------------------------------------------- #
# The screens
# --------------------------------------------------------------------------- #


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _message(text: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), text=text)


def _callback(data: str, message: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), data=data, message=message)


def _said(message: SimpleNamespace) -> str:
    return str(message.answer.await_args.args[0])


@pytest.fixture(autouse=True)
def _messages_are_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "Message", SimpleNamespace)
    _ = Message


def test_support_now_leads_somewhere() -> None:
    callbacks = [b.callback_data for row in support_keyboard().inline_keyboard for b in row]
    assert DisruptionCB.START in callbacks


@pytest.mark.asyncio
async def test_two_taps_file_a_report(session: AsyncSession) -> None:
    user = await _user(session)
    state = _state()
    message = _message()
    settings = SimpleNamespace(support_handle="", support_hours="")

    await handlers.disruption_start(_callback(DisruptionCB.START, message), state)  # type: ignore[arg-type]
    assert _said(message) == texts.disruption_pick_isp
    await handlers.disruption_isp(_callback(f"{DisruptionCB.ISP_PREFIX}rightel", message), state)  # type: ignore[arg-type]
    assert _said(message) == texts.disruption_pick_city
    await handlers.disruption_city_button(
        _callback(f"{DisruptionCB.CITY_PREFIX}{CITIES.index('کرج')}", message),  # type: ignore[arg-type]
        state,
        session,
        settings,  # type: ignore[arg-type]
        user,
    )

    assert _said(message) == texts.disruption_thanks
    assert await state.get_data() == {}
    [row] = await _reports(session)
    assert (row.isp, row.city) == ("rightel", "کرج")


@pytest.mark.asyncio
async def test_a_typed_city_is_retried_until_it_is_a_city(session: AsyncSession) -> None:
    user = await _user(session)
    state = _state()
    message = _message()
    settings = SimpleNamespace(support_handle="", support_hours="")
    await handlers.disruption_isp(_callback(f"{DisruptionCB.ISP_PREFIX}hiweb", message), state)  # type: ignore[arg-type]
    await handlers.disruption_write_city(_callback(DisruptionCB.CITY_WRITE, message), state)  # type: ignore[arg-type]
    assert await state.get_state() == DisruptionStates.waiting_city.state

    wrong = _message("0912 345")
    await handlers.disruption_city_typed(wrong, state, session, settings, user)  # type: ignore[arg-type]
    assert _said(wrong) == texts.disruption_bad_city
    assert await state.get_state() == DisruptionStates.waiting_city.state, "only retype the city"
    assert await _reports(session) == []

    right = _message("رشت")
    await handlers.disruption_city_typed(right, state, session, settings, user)  # type: ignore[arg-type]
    assert _said(right) == texts.disruption_thanks
    assert await state.get_state() is None
    assert [(r.isp, r.city) for r in await _reports(session)] == [("hiweb", "رشت")]


@pytest.mark.asyncio
async def test_a_stale_city_button_without_an_isp_files_nothing(session: AsyncSession) -> None:
    user = await _user(session)
    message = _message()
    await handlers.disruption_city_button(
        _callback(f"{DisruptionCB.CITY_PREFIX}0", message),  # type: ignore[arg-type]
        _state(),
        session,
        SimpleNamespace(),  # type: ignore[arg-type]
        user,
    )
    assert _said(message) == texts.disruption_lost
    assert await session.scalar(select(func.count()).select_from(DisruptionReport)) == 0
