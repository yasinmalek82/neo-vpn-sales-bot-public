"""A busy loser must not fail before the winner has had time to commit."""

from sqlalchemy import func, select

from database.models import User, WalletLedgerEntry
from database.models.report import ReportOutbox
from services import customer_lookup
from tests.unit.test_web_panel_customers import seed_user


async def test_wallet_replay_waits_for_a_winner_that_has_not_committed_yet(
    two_sessions, monkeypatch
):
    factory, _ = two_sessions
    uid = await seed_user(factory, 99001, balance=0)
    args = {
        "user_id": uid,
        "amount_toman": 5000,
        "admin_telegram_id": 1,
        "stamp": "web:busy-winner",
    }
    async with factory() as loser, factory() as winner:
        # Pin a real WAL read snapshot, then let another connection own the write.
        await loser.get(User, uid)
        await customer_lookup.adjust_wallet_manually(winner, **args)
        lookup = customer_lookup._entry_by_key
        misses = 0

        async def commit_after_the_first_empty_replay_read(session, key):
            nonlocal misses
            entry = await lookup(session, key)
            if session is loser and entry is None:
                misses += 1
                if misses == 2:
                    await winner.commit()
            return entry

        monkeypatch.setattr(
            customer_lookup, "_entry_by_key", commit_after_the_first_empty_replay_read
        )
        outcome = await customer_lookup.apply_wallet_adjustment(loser, **args)
        assert outcome.created is False
    async with factory() as session:
        assert (await session.get(User, uid)).wallet_balance_toman == 5000
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 1


async def test_busy_retry_is_bounded_and_holds_no_read_snapshot_while_waiting(
    two_sessions, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import pytest
    from sqlalchemy.exc import OperationalError

    factory, _ = two_sessions
    uid = await seed_user(factory, 99002, balance=0)
    args = {
        "user_id": uid,
        "amount_toman": 5000,
        "admin_telegram_id": 1,
        "stamp": "web:busy-bounded",
    }
    delays = []
    async with factory() as loser, factory() as winner:
        await loser.get(User, uid)
        await customer_lookup.adjust_wallet_manually(winner, **args)

        async def yielding_sleep(seconds):
            assert not loser.in_transaction()
            delays.append(seconds)
            await asyncio.sleep(0)

        monkeypatch.setattr(customer_lookup, "asyncio", SimpleNamespace(sleep=yielding_sleep))
        with pytest.raises(OperationalError):
            await asyncio.wait_for(
                customer_lookup.apply_wallet_adjustment(loser, **args), timeout=2
            )
        assert len(delays) == 5
    async with factory() as session:
        assert (await session.get(User, uid)).wallet_balance_toman == 0
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0
