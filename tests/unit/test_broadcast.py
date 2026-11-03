"""Broadcast: exactly once, resumable, and never to the wrong audience.

A campaign reaches real customers, so the properties that matter are: nobody
gets it twice, an interruption resumes instead of restarting, someone who
blocked the bot is not retried forever, and an admin never receives their own
marketing.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.broadcast import (
    BroadcastAudience,
    BroadcastState,
    BroadcastTarget,
    DeliveryState,
)
from database.models.pasarguard_account import PasarGuardAccount
from services.broadcast import (
    MAX_BODY_CHARS,
    BroadcastError,
    cancel_broadcast,
    count_audience,
    create_broadcast,
    get_progress,
    run_pending_broadcasts,
)


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
    """Records sends; can block a chat or demand a retry."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.bodies: list[str] = []
        self.blocked_chats: set[int] = set()
        self.retry_after_once = False

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:  # noqa: ANN003
        assert not kwargs, f"broadcast should send plain messages, got {kwargs}"
        if self.retry_after_once:
            self.retry_after_once = False
            raise TelegramRetryAfter(
                method=None, message="flood", retry_after=0  # type: ignore[arg-type]
            )
        if chat_id in self.blocked_chats:
            raise TelegramForbiddenError(method=None, message="blocked")  # type: ignore[arg-type]
        self.sent.append(chat_id)
        self.bodies.append(text)


async def _user(
    session: AsyncSession,
    telegram_id: int,
    *,
    admin: bool = False,
    banned: bool = False,
) -> User:
    user = User(
        telegram_id=telegram_id,
        referral_code=f"R{telegram_id}",
        is_admin=admin,
        is_banned=banned,
    )
    session.add(user)
    await session.flush()
    return user


async def _give_service(session: AsyncSession, user: User) -> None:
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_username=f"svc_{user.telegram_id}",
            panel_user_id=int(user.telegram_id),
        )
    )
    await session.flush()


async def _give_expiring_order(session: AsyncSession, user: User, *, days: int) -> None:
    plan = Plan(name=f"p{user.telegram_id}", data_limit_gb=50, duration_days=30, price_toman=1000)
    session.add(plan)
    await session.flush()
    session.add(
        Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=1000,
            expires_at=datetime.now(UTC) + timedelta(days=days),
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Audience
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admins_and_banned_users_are_never_targeted(
    session: AsyncSession,
) -> None:
    """An admin does not need marketing about their own product, and a banned
    user cannot use the bot at all."""
    await _user(session, 1)
    await _user(session, 2, admin=True)
    await _user(session, 3, banned=True)
    await session.commit()

    assert await count_audience(session, BroadcastAudience.all_customers) == 1


@pytest.mark.asyncio
async def test_service_filters_split_the_audience(session: AsyncSession) -> None:
    with_svc = await _user(session, 1)
    await _give_service(session, with_svc)
    await _user(session, 2)
    await session.commit()

    assert await count_audience(session, BroadcastAudience.with_service) == 1
    assert await count_audience(session, BroadcastAudience.without_service) == 1
    assert await count_audience(session, BroadcastAudience.all_customers) == 2


@pytest.mark.asyncio
async def test_expiring_soon_uses_the_same_window_as_the_reminder(
    session: AsyncSession,
) -> None:
    soon = await _user(session, 1)
    await _give_expiring_order(session, soon, days=3)
    later = await _user(session, 2)
    await _give_expiring_order(session, later, days=60)
    gone = await _user(session, 3)
    await _give_expiring_order(session, gone, days=-5)  # already expired
    await session.commit()

    assert await count_audience(session, BroadcastAudience.expiring_soon) == 1


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_creating_freezes_the_audience_into_rows(session: AsyncSession) -> None:
    """A customer who signs up mid-send must not receive a campaign that was
    never aimed at them."""
    await _user(session, 1)
    await _user(session, 2)
    await session.commit()

    broadcast = await create_broadcast(
        session, body="سلام", audience=BroadcastAudience.all_customers
    )
    await session.commit()

    await _user(session, 3)  # joins after the snapshot
    await session.commit()

    targets = (
        (
            await session.execute(
                select(BroadcastTarget).where(BroadcastTarget.broadcast_id == broadcast.id)
            )
        )
        .scalars()
        .all()
    )
    assert {t.telegram_id for t in targets} == {1, 2}


@pytest.mark.asyncio
async def test_an_empty_or_oversized_message_is_refused(session: AsyncSession) -> None:
    await _user(session, 1)
    await session.commit()
    for body in ("", "   ", "x" * (MAX_BODY_CHARS + 1)):
        with pytest.raises(BroadcastError):
            await create_broadcast(session, body=body, audience=BroadcastAudience.all_customers)


@pytest.mark.asyncio
async def test_an_empty_audience_is_refused(session: AsyncSession) -> None:
    """Better an explicit error than a campaign that silently reaches nobody."""
    await _user(session, 1, admin=True)
    await session.commit()
    with pytest.raises(BroadcastError):
        await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_campaign_reaches_everyone_once(session: AsyncSession) -> None:
    for tg in (1, 2, 3):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    progress = await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert progress is not None
    assert sorted(bot.sent) == [1, 2, 3]
    assert progress.sent == 3 and progress.done
    # Everyone gets the text the admin wrote, unchanged.
    assert set(bot.bodies) == {"سلام"}


@pytest.mark.asyncio
async def test_running_again_does_not_resend(session: AsyncSession) -> None:
    for tg in (1, 2):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    first = list(bot.sent)
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert bot.sent == first


@pytest.mark.asyncio
async def test_an_interrupted_send_resumes_where_it_stopped(
    session: AsyncSession,
) -> None:
    """Batching is what a restart resumes from; progress lives per recipient."""
    for tg in range(1, 6):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    await run_pending_broadcasts(session, bot, batch=2)  # type: ignore[arg-type]
    assert len(bot.sent) == 2
    progress = await get_progress(session, 1)
    assert progress.pending == 3

    await run_pending_broadcasts(session, bot, batch=2)  # type: ignore[arg-type]
    await run_pending_broadcasts(session, bot, batch=2)  # type: ignore[arg-type]
    assert sorted(bot.sent) == [1, 2, 3, 4, 5]
    assert len(bot.sent) == len(set(bot.sent)), "nobody received it twice"


@pytest.mark.asyncio
async def test_a_blocked_customer_is_recorded_and_not_retried(
    session: AsyncSession,
) -> None:
    for tg in (1, 2):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    bot.blocked_chats = {2}
    progress = await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert progress is not None
    assert progress.sent == 1 and progress.blocked == 1

    before = len(bot.sent)
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert len(bot.sent) == before, "a blocked customer must not be retried"


@pytest.mark.asyncio
async def test_a_rate_limit_pauses_the_batch_without_losing_anyone(
    session: AsyncSession,
) -> None:
    """Pushing through a flood-wait gets the bot limited for longer."""
    for tg in (1, 2, 3):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    bot.retry_after_once = True
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    # The throttled recipient stays pending rather than being marked failed.
    assert (await get_progress(session, 1)).pending >= 1

    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert sorted(bot.sent) == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_finished_campaign_is_marked_done(session: AsyncSession) -> None:
    await _user(session, 1)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    assert (await get_progress(session, 1)).state is BroadcastState.done
    assert await run_pending_broadcasts(session, bot) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancelling_stops_further_sends(session: AsyncSession) -> None:
    """Already-delivered messages stay delivered; the rest are not sent."""
    for tg in range(1, 6):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    await run_pending_broadcasts(session, bot, batch=2)  # type: ignore[arg-type]
    delivered = len(bot.sent)

    progress = await cancel_broadcast(session, 1)
    await session.commit()
    assert progress.state is BroadcastState.cancelled

    assert await run_pending_broadcasts(session, bot) is None  # type: ignore[arg-type]
    assert len(bot.sent) == delivered


@pytest.mark.asyncio
async def test_cancelling_a_finished_campaign_is_an_explicit_error(
    session: AsyncSession,
) -> None:
    await _user(session, 1)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()
    bot = FakeBot()
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]

    with pytest.raises(BroadcastError):
        await cancel_broadcast(session, 1)


@pytest.mark.asyncio
async def test_delivery_states_are_recorded_for_the_progress_screen(
    session: AsyncSession,
) -> None:
    for tg in (1, 2):
        await _user(session, tg)
    await session.commit()
    await create_broadcast(session, body="سلام", audience=BroadcastAudience.all_customers)
    await session.commit()

    bot = FakeBot()
    bot.blocked_chats = {2}
    await run_pending_broadcasts(session, bot)  # type: ignore[arg-type]

    rows = (await session.execute(select(BroadcastTarget))).scalars().all()
    states = {r.telegram_id: r.state for r in rows}
    assert states[1] is DeliveryState.sent
    assert states[2] is DeliveryState.blocked
    assert next(r for r in rows if r.telegram_id == 2).last_error
