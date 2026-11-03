"""``services/marketing_settings.py``: the moment a campaign was switched on."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.base import Base
from database.models.marketing import CAMPAIGNS, MarketingSettings
from services.marketing_settings import MarketingSettingsError, load_config, set_campaign

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_row_means_every_campaign_is_off_and_reading_writes_nothing(
    session: AsyncSession,
) -> None:
    config = await load_config(session)
    assert not config.any_on
    assert all(config.since(name) is None for name in CAMPAIGNS)
    assert await session.get(MarketingSettings, 1) is None


@pytest.mark.asyncio
async def test_switching_on_stores_the_moment_and_only_for_that_campaign(
    session: AsyncSession,
) -> None:
    await set_campaign(session, campaign="winback", on=True, now=T0)
    config = await load_config(session)
    assert config.since("winback") == T0
    assert config.is_on("winback") and not config.is_on("cart")


@pytest.mark.asyncio
async def test_switching_on_again_keeps_the_first_moment(session: AsyncSession) -> None:
    await set_campaign(session, campaign="cart", on=True, now=T0)
    await set_campaign(session, campaign="cart", on=True, now=T0 + timedelta(hours=5))
    assert (await load_config(session)).since("cart") == T0


@pytest.mark.asyncio
async def test_off_then_on_starts_a_new_moment(session: AsyncSession) -> None:
    """A campaign must not act on what happened while it was off."""
    await set_campaign(session, campaign="upgrade", on=True, now=T0)
    await set_campaign(session, campaign="upgrade", on=False)
    assert not (await load_config(session)).is_on("upgrade")
    later = T0 + timedelta(days=3)
    await set_campaign(session, campaign="upgrade", on=True, now=later)
    assert (await load_config(session)).since("upgrade") == later


@pytest.mark.asyncio
async def test_an_unknown_campaign_is_refused(session: AsyncSession) -> None:
    with pytest.raises(MarketingSettingsError):
        await set_campaign(session, campaign="spam", on=True)
