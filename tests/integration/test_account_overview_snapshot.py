"""«سرویس‌های من» must not hold a read snapshot across the panel call.

``get_account_overview`` reads the accounts and orders, awaits the panel's
``get_user``, then may write the subscription URL the panel returned. A
scheduler job committing during that await left the session on a stale WAL
snapshot, and the write died with «database is locked»
(SQLITE_BUSY_SNAPSHOT; ``busy_timeout`` does not apply).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import PasarGuardAccount, User
from services.accounts import get_account_overview

Factory = async_sessionmaker[AsyncSession]
SUB_URL = "https://panel.example/sub/abc"


async def _seed(factory: Factory) -> int:
    async with factory() as session:
        owner = User(telegram_id=4242, first_name="Ali", referral_code="OVER0001")
        session.add(owner)
        session.add(User(telegram_id=9, first_name="Other", referral_code="OVER0002"))
        await session.flush()
        session.add(
            PasarGuardAccount(
                user_id=owner.id,
                panel_user_id=77,
                panel_username="neo_overview",
                subscription_url=None,
            )
        )
        await session.commit()
        return int(owner.id)


class _PanelWhileAJobCommits:
    """``get_user`` takes long enough for a scheduler job to commit meanwhile."""

    def __init__(self, factory: Factory) -> None:
        self._factory = factory

    async def get_user(self, username: str) -> Any:
        async with self._factory() as other:
            row = await other.scalar(select(User).where(User.telegram_id == 9))
            row.first_name = f"Other {datetime.now(UTC).timestamp()}"
            await other.commit()
        return SimpleNamespace(
            username=username, status=None, expire=None, used_traffic=0, data_limit=0
        )

    def subscription_url_from_response(self, live: Any) -> str:  # noqa: ARG002
        return SUB_URL


@pytest.mark.asyncio
async def test_a_job_committing_during_the_panel_call_does_not_lock_the_overview(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    user_id = await _seed(factory)

    async with factory() as session:
        overview = await get_account_overview(
            session,
            user_id=user_id,
            client=_PanelWhileAJobCommits(factory),  # type: ignore[arg-type]
        )
        await session.commit()

    assert overview.subscription_url == SUB_URL
    async with factory() as check:
        stored = await check.scalar(
            select(PasarGuardAccount.subscription_url).where(
                PasarGuardAccount.panel_username == "neo_overview"
            )
        )
    assert stored == SUB_URL
