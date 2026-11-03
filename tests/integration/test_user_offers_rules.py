"""M3a, second audit: an offer is promised only to a customer who can use it.

* A1: a trial-ended or winback candidate with an open order gets nothing this run
  (no message, no offer row) and is looked at again on the next one. The cart reminder
  is about the open order itself and is still sent.
* A3: an offer is live only while its message is out. A transient send failure voids
  it with the released claim, the retry makes it live again; a refusal for good stays
  voided.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, OrderStatus, User
from services.marketing_run import MAX_SENDS_PER_RUN, run_hourly_nudges
from services.user_offers import best_live_offer
from tests.integration.marketing_helpers import NOW, make_order, make_plan
from tests.integration.test_marketing_jobs import (
    ON_SINCE,
    FakeBot,
    FakePanel,
    _cart,
    _ended_trial,
    _hourly,
    _keys,
    _open_order,
    _paid_service,
    _settings,
    _switch_on,
)
from tests.integration.test_user_offers_nudges import (
    FlakyBot,
    _aware,
    _offers,
    _set_offer,
)

Factory = async_sessionmaker[AsyncSession]


async def _hold_an_open_order(
    session: AsyncSession, user: User, status: OrderStatus = OrderStatus.PENDING_PAYMENT
) -> int:
    plan = await make_plan(session)
    order = await make_order(session, user, plan, status=status, price=150_000)
    return int(order.id)


async def _set_status(factory: Factory, order_id: int, status: OrderStatus) -> None:
    async with factory() as session:
        await session.execute(update(Order).where(Order.id == order_id).values(status=status))
        await session.commit()


# --- A1: no promise to a customer with an open order -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW])
async def test_a_trial_candidate_with_an_open_order_gets_nothing_until_the_order_is_dead(
    two_sessions: tuple[Factory, str], status: OrderStatus
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _ended_trial(session, 1, ended_hours_ago=30)
        order_id = await _hold_an_open_order(session, user, status)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)
    bot = FakeBot()

    assert await _hourly(factory, bot) == 0
    assert bot.sent == [] and await _offers(factory) == [] and await _keys(factory) == []

    await _set_status(factory, order_id, OrderStatus.EXPIRED)
    assert await _hourly(factory, bot) == 1, "looked at again next run, and sent"
    assert "تخفیف" in bot.sent[0][1]
    assert len(await _offers(factory)) == 1


@pytest.mark.asyncio
async def test_a_winback_candidate_with_an_open_order_gets_nothing_until_the_order_is_dead(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        order_id = await _hold_an_open_order(session, user)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    await _set_offer(factory, "winback", 20, 72)
    bot = FakeBot()
    panel = FakePanel({"svc_a": "expired"})

    assert await _hourly(factory, bot, panel) == 0
    assert bot.sent == [] and await _offers(factory) == [] and await _keys(factory) == []
    assert panel.asked == [], "refused before the panel is asked"

    await _set_status(factory, order_id, OrderStatus.EXPIRED)
    assert await _hourly(factory, bot, panel) == 1
    assert "تخفیف" in bot.sent[0][1]
    assert len(await _offers(factory)) == 1


@pytest.mark.asyncio
async def test_candidates_skipped_for_an_open_order_do_not_use_up_the_budget(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        # Older ends sort first, so the skipped ones are met before the sendable ones.
        for index in range(3):
            user = await _ended_trial(session, 100 + index, ended_hours_ago=100)
            await _hold_an_open_order(session, user)
        for index in range(MAX_SENDS_PER_RUN):
            await _ended_trial(session, 200 + index, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == MAX_SENDS_PER_RUN
    assert len(await _keys(factory)) == MAX_SENDS_PER_RUN
    assert {chat_id for chat_id, _text, _markup in bot.sent} == set(
        range(200, 200 + MAX_SENDS_PER_RUN)
    ), "every send went to a customer without an open order"


@pytest.mark.asyncio
async def test_the_cart_reminder_is_still_sent_to_a_customer_with_an_open_order(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        _user, order_id = await _open_order(session, 1, minutes_old=20)
        await session.commit()
    await _switch_on(factory, cart=ON_SINCE, trial_convert=ON_SINCE, winback=ON_SINCE)
    bot = FakeBot()
    assert await _cart(factory, bot) == 1
    assert await _keys(factory) == [f"mkt:cart:{order_id}"]


# --- A3: an offer is live only while its message is out ---------------------------------------


@pytest.mark.asyncio
async def test_a_failed_send_takes_the_offer_back_and_the_retry_gives_it_again(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)

    assert await _hourly(factory, FlakyBot(RuntimeError("telegram hiccup"))) == 0
    first = await _offers(factory)
    assert len(first) == 1 and first[0].voided_at is not None
    async with factory() as session:
        assert await best_live_offer(session, user_id=user.id, volume_gb=None, now=NOW) is None

    later = NOW + timedelta(hours=1)
    bot = FakeBot()
    sent = await run_hourly_nudges(
        session_factory=factory,
        bot=bot,
        settings=_settings(),
        pause_seconds=0,
        client=FakePanel(),
        now=later,
    )
    assert sent == 1 and "تخفیف" in bot.sent[0][1]
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].id == first[0].id
    assert rows[0].voided_at is None
    assert _aware(rows[0].expires_at) == later + timedelta(hours=24)
    async with factory() as session:
        live = await best_live_offer(session, user_id=user.id, volume_gb=None, now=later)
    assert live is not None and live.id == rows[0].id


@pytest.mark.asyncio
async def test_a_failed_winback_send_also_takes_the_offer_back(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    await _set_offer(factory, "winback", 20, 72)
    assert (
        await _hourly(factory, FlakyBot(RuntimeError("timeout")), FakePanel({"svc_a": "expired"}))
        == 0
    )
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].voided_at is not None
    async with factory() as session:
        assert await best_live_offer(session, user_id=user.id, volume_gb=None, now=NOW) is None


@pytest.mark.asyncio
async def test_a_refusal_for_good_stays_voided_and_is_never_revived(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)
    blocked = TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked by the user"
    )
    assert await _hourly(factory, FlakyBot(blocked)) == 0
    bot = FakeBot()
    assert await _hourly(factory, bot) == 0, "refused for good: the claim is kept, not retried"
    assert bot.sent == []
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].voided_at is not None
