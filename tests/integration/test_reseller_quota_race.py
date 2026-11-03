"""One free reseller-trial slot must not be handed out twice."""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Order, PasarGuardAccount, User
from database.models.order import TRIAL_KIND_RESELLER
from services.reseller import set_rate
from services.trial import TrialError, get_reseller_trial_quota, issue_reseller_trial

_account_ids = itertools.count(1)


async def _provision_without_committing(
    session: AsyncSession,
    _client: object,
    *,
    order_id: int,
    default_group_ids: object = None,
) -> tuple[PasarGuardAccount, None]:
    """Keep the caller's transaction open.

    The real provisioner commits the panel username before panel I/O. That
    commit drops the shop-row lock while the giveaway is still writing, and on
    SQLite the next shop's lock write then blocks the whole file. This stub
    only records the account, so the lock lasts until the caller commits.
    """
    _ = default_group_ids
    order = await session.get(Order, order_id)
    assert order is not None
    number = next(_account_ids)
    account = PasarGuardAccount(
        user_id=order.user_id,
        panel_user_id=number,
        panel_username=f"{order.service_name}_{number}",
        subscription_url=f"https://panel.example.invalid/sub/{number}",
    )
    session.add(account)
    await session.flush()
    return account, None


@pytest.mark.asyncio
async def test_one_free_slot_cannot_be_issued_twice(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second giveaway waits out the first transaction, then finds the slot gone."""
    factory, _url = two_sessions
    async with factory() as session:
        shop = User(
            telegram_id=880,
            username="shop",
            first_name="S",
            last_name=None,
            language_code="fa",
            referral_code="SHOP0880",
            is_admin=False,
            is_reseller=True,
        )
        session.add(shop)
        await session.flush()
        await set_rate(
            session,
            shop.id,
            price_per_gb_toman=0,
            price_per_day_toman=0,
            trial_quota_monthly=1,
            trial_price_toman=0,
        )
        await session.commit()
        user_id = int(shop.id)

    real_quota = get_reseller_trial_quota
    first_inside = asyncio.Event()
    release_first = asyncio.Event()

    async def overlap_the_read(
        session: AsyncSession, reseller_id: int, *, now: datetime | None = None
    ):
        quota = await real_quota(session, reseller_id, now=now)
        if not first_inside.is_set():
            first_inside.set()
            await release_first.wait()
        return quota

    monkeypatch.setattr("services.trial.get_reseller_trial_quota", overlap_the_read)
    monkeypatch.setattr("services.trial.provision_order", _provision_without_committing)

    async def issue(name: str) -> str:
        async with factory() as session:
            try:
                await issue_reseller_trial(
                    session,
                    AsyncMock(),
                    user_id=user_id,
                    data_limit_gb=1,
                    duration_days=1,
                    hwid_limit=1,
                    group_ids=[1],
                    service_name=name,
                    display_name=name,
                )
                await session.commit()
            except TrialError as exc:
                await session.rollback()
                return str(exc)
            return "ok"

    async def release_after_the_other_has_started() -> None:
        await first_inside.wait()
        await asyncio.sleep(0.1)
        release_first.set()

    first, second, _release = await asyncio.gather(
        issue("ali"),
        issue("reza"),
        release_after_the_other_has_started(),
    )
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.user_id == user_id, Order.trial_kind == TRIAL_KIND_RESELLER)
        )
    assert int(count or 0) == 1
    assert sorted((first, second)) == ["ok", "trial_quota_exhausted"]
