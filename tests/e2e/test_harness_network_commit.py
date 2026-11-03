"""The opt-in mode that plays a scheduler job at every network call.

Live, «database is locked» came from one shape: read, await Telegram or the
panel, write on the same snapshot while a job committed in between. These pin
that the mode really catches that shape and leaves the correct one alone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from database.base import end_read_transaction
from database.models import User
from tests.e2e.harness import App


async def test_a_write_on_a_snapshot_held_across_a_network_call_is_caught(app: App) -> None:
    admin = app.admin()
    await admin.send("/admin")  # the first update writes the admin's row
    app.commit_at_every_network_call()

    async with app.session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == admin.user_id))
        await app.bot.get_me()
        user.first_name = "held"
        with pytest.raises(OperationalError, match="database is locked"):
            await session.flush()

    assert app.network_commits >= 1


async def test_a_write_after_the_snapshot_was_released_goes_through(app: App) -> None:
    admin = app.admin()
    await admin.send("/admin")  # the first update writes the admin's row
    app.commit_at_every_network_call()

    async with app.session() as session:
        await session.scalar(select(User).where(User.telegram_id == admin.user_id))
        assert await end_read_transaction(session)
        await app.bot.get_me()
        user = await session.scalar(select(User).where(User.telegram_id == admin.user_id))
        user.first_name = "released"
        await session.commit()

    assert await app.one(select(User.first_name).where(User.telegram_id == admin.user_id)) == (
        "released"
    )


async def test_the_mode_is_off_unless_asked_for(app: App) -> None:
    if app.tg.on_network is not None:
        pytest.skip("E2E_COMMIT_AT_NETWORK=1 is set for this run")
    await app.bot.get_me()
    assert app.network_commits == 0
