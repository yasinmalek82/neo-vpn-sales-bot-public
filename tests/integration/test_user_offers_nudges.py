"""M3a: a nudge that carries a personal offer writes the offer with the claim.

The offer row is keyed by the notice's ``event_key``, written in the transaction
that claims the notice, and voided when the recipient refuses the message for
good. With ``offer_percent`` 0 there is no row and no line.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.texts import texts
from database.models import UserOffer
from database.models.marketing import MarketingSettings
from services.marketing_run import run_hourly_nudges
from services.ux_format import to_persian_digits
from tests.integration.marketing_helpers import NOW
from tests.integration.test_marketing_jobs import (
    ON_SINCE,
    FakeBot,
    FakePanel,
    _ended_trial,
    _hourly,
    _paid_service,
    _settings,
    _switch_on,
)

Factory = async_sessionmaker[AsyncSession]


class FlakyBot(FakeBot):
    """The first sends fail with ``error``; later ones go through."""

    def __init__(self, error: Exception, failures: int = 1) -> None:
        super().__init__()
        self.error = error
        self.failures = failures

    async def send_message(self, *, chat_id: int, text: str, reply_markup: Any = None) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        await super().send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def _line(hours: int, percent: int) -> str:
    return texts.notify_offer_line.format(
        hours=to_persian_digits(hours), percent=to_persian_digits(percent)
    )


async def _offers(factory: Factory) -> list[UserOffer]:
    async with factory() as session:
        return list((await session.scalars(select(UserOffer).order_by(UserOffer.id))).all())


async def _set_offer(factory: Factory, campaign: str, percent: int, hours: int) -> None:
    async with factory() as session:
        row = await session.get(MarketingSettings, 1)
        assert row is not None
        setattr(row, f"{campaign}_offer_percent", percent)
        setattr(row, f"{campaign}_offer_hours", hours)
        await session.commit()


def _aware(moment: Any) -> Any:
    from datetime import UTC

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_with_percent_zero_the_message_is_unchanged_and_no_offer_exists(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 1
    assert bot.sent[0][1] == texts.notify_trial_convert
    assert "تخفیف" not in bot.sent[0][1]
    assert await _offers(factory) == []


@pytest.mark.asyncio
async def test_a_trial_nudge_with_a_percent_writes_one_row_and_adds_the_line(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)
    bot = FakeBot()
    assert await _hourly(factory, bot) == 1

    text = bot.sent[0][1]
    assert text.startswith(texts.notify_trial_convert)
    assert _line(24, 15) in text
    rows = await _offers(factory)
    assert len(rows) == 1
    offer = rows[0]
    assert offer.event_key == f"mkt:trial_convert:{user.id}"
    assert offer.user_id == user.id and offer.campaign == "trial_convert"
    assert offer.percent == 15 and offer.min_volume_gb is None
    assert _aware(offer.expires_at) == NOW + timedelta(hours=24)
    assert offer.order_id is None and offer.voided_at is None

    assert await _hourly(factory, bot) == 0
    assert len(await _offers(factory)) == 1 and len(bot.sent) == 1


@pytest.mark.asyncio
async def test_a_winback_nudge_with_a_percent_writes_one_row_and_adds_the_line(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        user = await _paid_service(session, 1, "svc_a", ended_days_ago=10)
        await session.commit()
    await _switch_on(factory, winback=ON_SINCE)
    await _set_offer(factory, "winback", 20, 72)
    bot = FakeBot()
    assert await _hourly(factory, bot, FakePanel({"svc_a": "expired"})) == 1
    assert _line(72, 20) in bot.sent[0][1]
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].campaign == "winback" and rows[0].percent == 20
    assert rows[0].event_key.startswith(f"mkt:winback:{user.id}:")
    assert _aware(rows[0].expires_at) == NOW + timedelta(hours=72)


@pytest.mark.asyncio
async def test_a_retried_send_reuses_the_row_and_moves_its_expiry_to_the_new_send(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)

    flaky = FlakyBot(RuntimeError("telegram hiccup"))
    assert await _hourly(factory, flaky) == 0, "the send failed"
    first = await _offers(factory)
    assert len(first) == 1 and _aware(first[0].expires_at) == NOW + timedelta(hours=24)

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
    assert sent == 1
    rows = await _offers(factory)
    assert len(rows) == 1, "a retry never makes a second row"
    assert rows[0].id == first[0].id
    assert _aware(rows[0].expires_at) == later + timedelta(hours=24)
    assert rows[0].voided_at is None


@pytest.mark.asyncio
async def test_a_recipient_who_blocked_the_bot_has_the_offer_voided(
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
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].voided_at is not None


@pytest.mark.asyncio
async def test_a_transient_failure_voids_the_offer_until_the_retry_goes_out(
    two_sessions: tuple[Factory, str],
) -> None:
    """Superseded rule (A3): the claim is released, so the offer goes with it."""
    factory, _ = two_sessions
    async with factory() as session:
        await _ended_trial(session, 1, ended_hours_ago=30)
        await session.commit()
    await _switch_on(factory, trial_convert=ON_SINCE)
    await _set_offer(factory, "trial_convert", 15, 24)
    assert await _hourly(factory, FlakyBot(RuntimeError("timeout"))) == 0
    rows = await _offers(factory)
    assert len(rows) == 1 and rows[0].voided_at is not None
