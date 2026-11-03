"""Records what a customer is told when a code with a rule meets a plan that takes no extra discount.

``resolve_price`` calls ``check_promo_eligibility`` before the branch that raises
``promo_not_applicable`` for a ready plan with ``forbid_extra_discount``. These tests pin the
order as it is today so the owner can decide whether it is the order he wants; they do not say
it is right. A valid code on such a plan is the control: it gets the «not applicable» answer.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import purchase
from database.base import Base
from services.promo_rules import PromoExpired, PromoUsedUp
from tests.unit.test_customer_purchase_flow import _customer
from tests.unit.test_promo_rules import (
    _entry,
    _iso,
    _quote,
    _redeem,
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
async def test_a_valid_code_on_a_no_extra_discount_plan_is_not_applicable(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8401)
    with pytest.raises(ValueError, match="promo_not_applicable"):
        await _quote(session, tmp_path, buyer, _entry(), forbid_extra_discount=True)


@pytest.mark.asyncio
async def test_current_order_an_expired_code_on_a_no_extra_discount_plan_raises_promo_expired(
    session: AsyncSession, tmp_path: Path
) -> None:
    buyer = await _customer(session, balance=0, telegram_id=8402)
    with pytest.raises(PromoExpired):
        await _quote(
            session,
            tmp_path,
            buyer,
            _entry(expires_at=_iso(-timedelta(hours=1))),
            forbid_extra_discount=True,
        )


@pytest.mark.asyncio
async def test_current_order_a_used_up_code_on_a_no_extra_discount_plan_raises_promo_used_up(
    session: AsyncSession, tmp_path: Path
) -> None:
    holder = await _customer(session, balance=0, telegram_id=8403)
    buyer = await _customer(session, balance=0, telegram_id=8404)
    await _redeem(session, holder)
    with pytest.raises(PromoUsedUp):
        await _quote(session, tmp_path, buyer, _entry(total_limit=1), forbid_extra_discount=True)
