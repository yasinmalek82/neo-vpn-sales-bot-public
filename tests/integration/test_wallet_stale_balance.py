"""A previously loaded User must not overwrite a newer wallet balance.

The requests use separate sessions and a temporary SQLite file; this same suite
can also be run against an explicitly authorized disposable PostgreSQL database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import User, WalletLedgerEntry
from services.wallet import InsufficientWalletFunds, credit, debit

Factory = async_sessionmaker[AsyncSession]


async def _seed(factory: Factory, balance: int = 1_000) -> int:
    async with factory() as session:
        user = User(
            telegram_id=222,
            referral_code="WALLETST01",
            first_name="Before",
            wallet_balance_toman=balance,
        )
        session.add(user)
        await session.commit()
        return int(user.id)


async def _balance_and_entries(factory: Factory, user_id: int) -> tuple[int, list[tuple[int, int]]]:
    async with factory() as session:
        balance = await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
        rows = (
            await session.execute(
                select(WalletLedgerEntry.amount_toman, WalletLedgerEntry.balance_after_toman)
                .where(WalletLedgerEntry.user_id == user_id)
                .order_by(WalletLedgerEntry.id)
            )
        ).all()
        assert balance is not None
        return int(balance), [(int(amount), int(after)) for amount, after in rows]


async def test_credit_uses_current_balance_even_if_user_is_cached(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory)
    async with factory() as first:
        cached = await first.get(User, user_id)
        assert cached is not None and cached.wallet_balance_toman == 1_000
        await first.commit()  # no open read snapshot, but the User object remains cached
        async with factory() as second:
            await credit(
                second, user_id=user_id, amount_toman=200, reason="test", idempotency_key="credit-a"
            )
            await second.commit()
        entry, created = await credit(
            first, user_id=user_id, amount_toman=300, reason="test", idempotency_key="credit-b"
        )
        assert created
        assert entry.balance_after_toman == 1_500
        await first.commit()
    assert await _balance_and_entries(factory, user_id) == (1_500, [(200, 1_200), (300, 1_500)])


async def test_debit_sees_funds_added_after_user_was_cached(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory, 100)
    async with factory() as first:
        cached = await first.get(User, user_id)
        assert cached is not None and cached.wallet_balance_toman == 100
        await first.commit()
        async with factory() as second:
            await credit(
                second, user_id=user_id, amount_toman=100, reason="test", idempotency_key="top-up"
            )
            await second.commit()
        entry, created = await debit(
            first, user_id=user_id, amount_toman=150, reason="test", idempotency_key="debit"
        )
        assert created and entry.balance_after_toman == 50
        await first.commit()
    assert await _balance_and_entries(factory, user_id) == (50, [(100, 200), (150, 50)])


async def test_debit_still_refuses_when_another_session_spent_the_balance(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory)
    async with factory() as first:
        cached = await first.get(User, user_id)
        assert cached is not None
        await first.commit()
        async with factory() as second:
            await debit(
                second, user_id=user_id, amount_toman=800, reason="test", idempotency_key="spent"
            )
            await second.commit()
        with pytest.raises(InsufficientWalletFunds):
            await debit(
                first, user_id=user_id, amount_toman=500, reason="test", idempotency_key="too-much"
            )
        await first.rollback()
    assert await _balance_and_entries(factory, user_id) == (200, [(800, 200)])


async def test_refreshing_the_balance_does_not_discard_other_pending_user_changes(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory)
    async with factory() as first:
        cached = await first.get(User, user_id)
        assert cached is not None
        await first.commit()
        async with factory() as second:
            await credit(
                second, user_id=user_id, amount_toman=200, reason="test", idempotency_key="other"
            )
            await second.commit()
        with first.no_autoflush:
            cached.first_name = "Updated"
            await credit(
                first, user_id=user_id, amount_toman=300, reason="test", idempotency_key="mine"
            )
        await first.commit()
    async with factory() as check:
        user = await check.get(User, user_id)
        assert user is not None and user.first_name == "Updated"
    assert await _balance_and_entries(factory, user_id) == (1_500, [(200, 1_200), (300, 1_500)])


async def test_multiple_adjustments_are_rolled_back_together(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory)
    async with factory() as session:
        await credit(session, user_id=user_id, amount_toman=200, reason="test", idempotency_key="a")
        await credit(session, user_id=user_id, amount_toman=300, reason="test", idempotency_key="b")
        await debit(session, user_id=user_id, amount_toman=400, reason="test", idempotency_key="c")
        await session.rollback()
    assert await _balance_and_entries(factory, user_id) == (1_000, [])


async def test_a_replayed_credit_does_not_apply_again_with_a_cached_user(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id = await _seed(factory)
    async with factory() as first:
        cached = await first.get(User, user_id)
        assert cached is not None
        await first.commit()
        async with factory() as second:
            original, _ = await credit(
                second, user_id=user_id, amount_toman=200, reason="test", idempotency_key="same"
            )
            original_id = original.id
            await second.commit()
        replay, created = await credit(
            first, user_id=user_id, amount_toman=200, reason="test", idempotency_key="same"
        )
        assert not created and replay.id == original_id
        await first.commit()
    assert await _balance_and_entries(factory, user_id) == (1_200, [(200, 1_200)])
