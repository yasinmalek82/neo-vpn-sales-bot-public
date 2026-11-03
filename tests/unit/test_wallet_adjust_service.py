"""The shared wallet-adjust pieces: the amount parser, the replay answer, the request key."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from database.base import Base
from database.models import User
from database.models.wallet import WalletLedgerEntry
from services.customer_lookup import (
    MAX_WALLET_TOMAN,
    ManualAdjustError,
    adjust_wallet_manually,
    apply_wallet_adjustment,
    parse_signed_toman,
)
from services.wallet_request_key import mint_request_token, stamp_of_request_token


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(session: AsyncSession, balance: int) -> User:
    user = User(
        telegram_id=1,
        username="u",
        first_name="U",
        language_code="fa",
        referral_code="R1",
        wallet_balance_toman=balance,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50000", 50_000),
        ("-20000", -20_000),
        ("+7", 7),
        ("۵۰٬۰۰۰", 50_000),
        ("٥٠٠٠", 5000),  # Arabic-Indic digits worked before and still do
        ("‎-1004336742277", -1004336742277),
        ("−1004336742277", -1004336742277),
        (" 1,000 ", 1000),
        (str(MAX_WALLET_TOMAN), MAX_WALLET_TOMAN),
        (str(MAX_WALLET_TOMAN + 1), None),
        ("9" * 40, None),
        ("²", None),  # int() raised on this one before
        ("abc", None),
        ("", None),
        ("-", None),
        ("1.5", None),
    ],
)
def test_the_amount_parser(raw: str, expected: int | None) -> None:
    assert parse_signed_toman(raw) == expected


@pytest.mark.asyncio
async def test_a_landed_deduction_repeated_after_the_balance_dropped_is_not_a_refusal(
    session: AsyncSession,
) -> None:
    """Red before the replay lookup moved ahead of the overdraft check: the repeat read
    «موجودی کافی نیست» although the money had moved exactly once."""
    user = await _user(session, 100)
    first = await adjust_wallet_manually(
        session, user_id=user.id, amount_toman=-100, admin_telegram_id=5, stamp="s"
    )
    await session.commit()
    again = await adjust_wallet_manually(
        session, user_id=user.id, amount_toman=-100, admin_telegram_id=5, stamp="s"
    )
    assert again.id == first.id
    assert int((await session.scalar(select(User.wallet_balance_toman))) or 0) == 0
    assert len((await session.scalars(select(WalletLedgerEntry))).all()) == 1
    # A different request for the same amount is still refused: the wallet is empty.
    with pytest.raises(ManualAdjustError):
        await adjust_wallet_manually(
            session, user_id=user.id, amount_toman=-100, admin_telegram_id=5, stamp="other"
        )


@pytest.mark.asyncio
async def test_apply_says_whether_this_call_wrote(session: AsyncSession) -> None:
    user = await _user(session, 0)
    kw = {"user_id": user.id, "amount_toman": 300, "admin_telegram_id": 5, "stamp": "web:k"}
    first = await apply_wallet_adjustment(session, **kw)
    second = await apply_wallet_adjustment(session, **kw)
    assert (first.created, second.created) == (True, False)
    assert first.entry.id == second.entry.id and second.balance_after_toman == 300


@pytest.mark.asyncio
async def test_apply_rolls_back_a_refusal(session: AsyncSession) -> None:
    user = await _user(session, 10)
    with pytest.raises(ManualAdjustError):
        await apply_wallet_adjustment(
            session, user_id=user.id, amount_toman=-50, admin_telegram_id=5, stamp="s"
        )
    assert (await session.scalars(select(WalletLedgerEntry))).all() == []


def test_a_token_belongs_to_one_admin_one_customer_one_amount() -> None:
    token = mint_request_token("secret", admin=1, user=2, amount=500)
    stamp = stamp_of_request_token("secret", token, admin=1, user=2, amount=500)
    assert stamp is not None and stamp.startswith("web:")
    assert stamp_of_request_token("secret", token, admin=1, user=2, amount=500) == stamp
    for kw in ({"admin": 9}, {"user": 3}, {"amount": 501}, {"amount": -500}):
        args = {"admin": 1, "user": 2, "amount": 500, **kw}
        assert stamp_of_request_token("secret", token, **args) is None, kw
    assert stamp_of_request_token("other-secret", token, admin=1, user=2, amount=500) is None
    for bad in (None, "", "x", "a.b", token + "0", token.replace(".", ":")):
        assert stamp_of_request_token("secret", bad, admin=1, user=2, amount=500) is None


def test_two_tokens_differ_and_the_ledger_key_fits_its_column() -> None:
    a = mint_request_token("s", admin=1, user=2, amount=5)
    assert a != mint_request_token("s", admin=1, user=2, amount=5)
    stamp = stamp_of_request_token("s", a, admin=1, user=2, amount=5)
    assert len(f"admin_adjust:{2**63}:{2**63}:{stamp}") <= 128  # String(128)
