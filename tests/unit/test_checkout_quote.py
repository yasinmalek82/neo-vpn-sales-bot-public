"""The summary and the till must charge one quote.

The fixed-price lock used to be applied inline in both screens, and the order
screen once omitted the reseller exception. These tests call the checkout
use-case, then prove both screens ask that use-case rather than deciding again.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from services.pricing import PriceResult


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        max_discount_percent=None,
        referral_referred_discount_percent=0,
    )


def _price(*, final: int, mode: str) -> PriceResult:
    return PriceResult(
        original_price=final,
        discount_amount=0,
        final_price=final,
        currency="IRT",
        meta={"pricing_mode": mode},
    )


@pytest.mark.asyncio
async def test_a_wrong_engine_price_is_replaced_by_the_fixed_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is inside the quote. Removing it leaves the engine's 999."""
    from services import checkout

    async def wrong_engine(*_args: object, **_kwargs: object) -> PriceResult:
        return _price(final=999, mode="fixed_ready")

    monkeypatch.setattr(checkout, "resolve_price", wrong_engine)
    priced = await checkout.quote_checkout(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        {"plan_type": "volume", "fixed_price_toman": 150_000},
        promo_code=None,
        db_user=None,
    )
    assert priced.final_price == 150_000


@pytest.mark.asyncio
async def test_the_lock_does_not_replace_a_reseller_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconditional rewrite charges the shop the retail 150_000."""
    from services import checkout

    async def agreed(*_args: object, **_kwargs: object) -> PriceResult:
        return _price(final=110_000, mode="reseller")

    monkeypatch.setattr(checkout, "resolve_price", agreed)
    priced = await checkout.quote_checkout(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        {"plan_type": "volume", "fixed_price_toman": 150_000},
        promo_code=None,
        db_user=None,
    )
    assert priced.final_price == 110_000


def _cfg_with_rates() -> dict[str, object]:
    """Retail custom-volume rate of 12,000/GB — the number the bug charged."""
    return {
        "currency": "IRT",
        "volume_plan": {
            "tiers": [{"max_gb": None, "price_per_gb_toman": "12000"}],
            "duration_discounts": [],
        },
        "promo_codes": {"OFF15": {"percent": "15", "active": True}},
        "sales_rules": {"discount_combine": "best_of_promo_vs_extra"},
    }


@pytest.mark.asyncio
async def test_a_promo_discounts_the_ready_plans_own_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """50 GB / 60 days at 255,000 with a 15% code must cost 216,750.

    It cost 510,000: with a code, checkout fell through to the custom-volume
    rate engine, re-priced the plan at 50 × 12,000 = 600,000 and took 15% off
    that — a «discount» that doubled the price.
    """
    from services import checkout

    async def cfg(*_args: object, **_kwargs: object) -> dict[str, object]:
        return _cfg_with_rates()

    monkeypatch.setattr(checkout, "load_pricing_config_async", cfg)
    priced = await checkout.quote_checkout(
        SimpleNamespace(),  # type: ignore[arg-type]
        _settings(),
        {
            "plan_type": "volume",
            "volume_gb": 50,
            "duration_days": 60,
            "fixed_price_toman": 255_000,
        },
        promo_code="off15",
        db_user=None,
    )
    assert priced.original_price == 255_000
    assert priced.discount_amount == 38_250
    assert priced.final_price == 216_750
    assert priced.meta["promo_code"] == "OFF15"


@pytest.mark.asyncio
async def test_a_disabled_promo_on_a_ready_plan_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import checkout

    doc = _cfg_with_rates()
    doc["promo_codes"] = {"OFF15": {"percent": "15", "active": False}}

    async def cfg(*_args: object, **_kwargs: object) -> dict[str, object]:
        return doc

    monkeypatch.setattr(checkout, "load_pricing_config_async", cfg)
    with pytest.raises(ValueError, match="invalid_promo_code"):
        await checkout.quote_checkout(
            SimpleNamespace(),  # type: ignore[arg-type]
            _settings(),
            {
                "plan_type": "volume",
                "volume_gb": 50,
                "duration_days": 60,
                "fixed_price_toman": 255_000,
            },
            promo_code="OFF15",
            db_user=None,
        )


class _State:
    def __init__(self) -> None:
        self.data = {
            "plan_type": "volume",
            "volume_gb": 10,
            "duration_days": 30,
            "fixed_price_toman": 150_000,
        }
        self.saved: dict[str, object] = {}

    async def get_data(self) -> dict[str, object]:
        return self.data

    async def update_data(self, **kwargs: object) -> None:
        self.saved.update(kwargs)

    async def set_state(self, _state: object) -> None:
        return None

    async def clear(self) -> None:
        return None


class _Message:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def answer(self, text: str, reply_markup: object = None) -> None:
        _ = reply_markup
        self.texts.append(text)


@pytest.mark.asyncio
async def test_the_summary_shows_the_checkout_quote(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private copy in the handler would ignore this quote."""
    from bot.handlers import purchase

    async def quoted(*_args: object, **_kwargs: object) -> PriceResult:
        return _price(final=110_000, mode="reseller")

    async def no_balance(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(purchase, "quote_checkout", quoted)
    monkeypatch.setattr(purchase, "get_balance", no_balance)
    message = _Message()
    state = _State()
    await purchase._show_summary(
        message,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        promo_code=None,
        db_user=None,
    )
    assert state.saved["final_price"] == 110_000


@pytest.mark.asyncio
async def test_confirm_asks_the_checkout_quote(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The till used to apply its own lock after resolving a price."""
    from bot.handlers import purchase

    user = User(telegram_id=910, referral_code="Q910")
    session.add(user)
    await session.flush()

    async def boom(*_args: object, **_kwargs: object) -> PriceResult:
        raise RuntimeError("quote_checkout")

    async def no_open(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(purchase, "quote_checkout", boom)
    monkeypatch.setattr(purchase, "resume_open_order_if_any", no_open)
    callback = SimpleNamespace(answer=_async_none, message=_Message())
    with pytest.raises(RuntimeError, match="quote_checkout"):
        await purchase.confirm_order(
            callback,  # type: ignore[arg-type]
            _State(),  # type: ignore[arg-type]
            session,
            _settings(),  # type: ignore[arg-type]
            user,
        )


async def _async_none(*_args: object, **_kwargs: object) -> None:
    return None
