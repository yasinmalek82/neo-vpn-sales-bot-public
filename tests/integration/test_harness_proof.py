"""Prove the two-session harness isolates uncommitted writes."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import User


@pytest.mark.asyncio
async def test_flushed_but_uncommitted_write_is_invisible_to_other_session(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Session B must not see session A's flush until a commit.

    HARNESS PROOF: change the ``flush`` below to ``commit`` and this test must
    go red. If it stays green, the harness is not isolating connections.
    """
    factory, _url = two_sessions

    async with factory() as session_a:
        user = User(
            telegram_id=4242,
            username="harness",
            first_name="H",
            last_name=None,
            language_code="fa",
            referral_code="HARNESS1",
            is_admin=False,
        )
        session_a.add(user)
        await session_a.flush()
        # Intentionally NOT committed. Swap flush→commit above to verify the
        # harness: session_b would then see the row and this assert would fail.
        flushed_id = int(user.id)

        async with factory() as session_b:
            found = await session_b.scalar(select(User).where(User.id == flushed_id))
            assert found is None, (
                "session B saw session A's uncommitted row — harness is broken "
                "(or you changed flush to commit while checking the proof)"
            )
