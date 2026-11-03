"""SUSPECT-FLOW-2 / MONEY-4: the confirm tap honours the summary the customer saw.

If the re-quoted price or the wallet split differs from what the summary
showed, nothing is written: the summary is shown again with the new numbers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import purchase
from bot.texts import texts
from database.base import Base
from database.models import Order
from tests.unit.test_customer_purchase_flow import (  # noqa: F401  (fixtures + helpers)
    FIXED,
    _callback,
    _customer,
    _Message,
    _patch_panel,
    _pricing_file,
    _purchase_data,
    _settings,
    _state,
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


@pytest.fixture(autouse=True)
def _group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(purchase, "resolve_groups_for_plan_type", AsyncMock(return_value=[1]))


@pytest.mark.asyncio
async def test_a_catalog_price_change_since_the_summary_writes_nothing(
    session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=2001)
    message = _Message()
    # The summary showed FIXED; the plan now costs more.
    state = await _state(
        _purchase_data(
            fixed_price_toman=FIXED + 10_000,
            final_price=FIXED,
            card_amount=FIXED,
            wallet_applied=0,
        )
    )

    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        state,
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )

    assert (await session.execute(select(Order))).first() is None
    assert message.texts[0] == texts.purchase_price_updated
    assert (await state.get_data())["final_price"] == FIXED + 10_000


@pytest.mark.asyncio
async def test_a_wallet_that_shrank_since_the_summary_writes_nothing(
    session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=2002)
    message = _Message()
    # The summary said the wallet covers everything; the balance is gone.
    state = await _state(_purchase_data(final_price=FIXED, card_amount=0, wallet_applied=FIXED))

    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        state,
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )

    assert (await session.execute(select(Order))).first() is None
    assert message.texts[0] == texts.purchase_price_updated
    assert (await state.get_data())["card_amount"] == FIXED


@pytest.mark.asyncio
async def test_an_unchanged_summary_still_confirms(
    session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch)
    user = await _customer(session, balance=0, telegram_id=2003)
    message = _Message()
    state = await _state(_purchase_data(final_price=FIXED, card_amount=FIXED, wallet_applied=0))

    await purchase.confirm_order(
        _callback(message),  # type: ignore[arg-type]
        state,
        session,
        _settings(_pricing_file(tmp_path)),  # type: ignore[arg-type]
        user,
    )

    assert len((await session.execute(select(Order))).scalars().all()) == 1
    assert texts.purchase_price_updated not in message.texts


@pytest.mark.asyncio
async def test_a_real_promo_on_a_no_extra_discount_plan_is_named_not_applicable(
    session, tmp_path
) -> None:
    from services import checkout

    with pytest.raises(ValueError, match="promo_not_applicable"):
        await checkout.quote_checkout(
            session,
            _settings(_pricing_file(tmp_path, promo=True)),
            _purchase_data(forbid_extra_discount=True),
            promo_code="off15",
            db_user=None,
        )
