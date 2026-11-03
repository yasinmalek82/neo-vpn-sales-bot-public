"""Disposable-database proof for ``o2p3q4r5s6t7`` (one trial per store group).

Never points Alembic at the customer database: ``DATABASE_URL`` is set only on
the child process, to a temporary SQLite file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Order, OrderStatus, Plan
from database.models.plan import DataLimitResetStrategy
from tests.integration.test_one_open_order_migration import (
    _alembic,
    _drop_legacy_sqlite_status_check,
    insert_order_row,
    insert_user_row,
)

_PARENT = "n1o2p3q4r5s6"
# Pinned, not "head": later revisions rebuild ``orders`` and would run over the
# hand-edited legacy schema these tests build.
_TARGET = "o2p3q4r5s6t7"


async def _seed(url: str, *, slugs: list[str | None]) -> int:
    """One user holding one personal trial per entry of ``slugs``."""
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user_id = int(
                (
                    await session.execute(
                        insert_user_row(
                            telegram_id=77,
                            username="tester",
                            first_name="T",
                            referral_code="TRIAL077",
                        )
                    )
                ).scalar_one()
            )
            plan = Plan(
                name="trial-1GB-1d",
                description="t",
                data_limit_gb=1,
                duration_days=1,
                price_toman=0,
                data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
                group_ids=[],
                hwid_limit=1,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
            for slug in slugs:
                values = {
                    "user_id": user_id,
                    "plan_id": int(plan.id),
                    "status": OrderStatus.ACTIVE.value,
                    "price_toman": 0,
                    "discount_toman": 0,
                    "wallet_applied_toman": 0,
                    "force_new": True,
                    "trial_kind": "personal",
                }
                if slug is not None:
                    values["trial_category_slug"] = slug
                await session.execute(insert_order_row(**values))
            await session.commit()
            return user_id
    finally:
        await engine.dispose()


async def _personal_count(url: str, user_id: int) -> int:
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.user_id == user_id, Order.trial_kind == "personal")
                )
                or 0
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_round_trips_and_keeps_a_pre_group_trial_as_null(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'rt.db'}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    _drop_legacy_sqlite_status_check(url)
    # At the parent revision a user holds exactly one personal trial.
    user_id = await _seed(url, slugs=[None])
    up = _alembic(url, "upgrade", _TARGET)
    assert up.returncode == 0, up.stderr

    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            row = (
                await session.execute(
                    select(Order.trial_category_slug, Order.trial_voided_at).where(
                        Order.user_id == user_id
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert tuple(row) == (None, None)


@pytest.mark.asyncio
async def test_downgrade_refuses_while_a_user_holds_two_trials(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'two.db'}"
    assert _alembic(url, "upgrade", _TARGET).returncode == 0
    _drop_legacy_sqlite_status_check(url)
    user_id = await _seed(url, slugs=["unlimited", "volume"])

    refused = _alembic(url, "downgrade", _PARENT)
    assert refused.returncode != 0
    assert "uq_orders_personal_trial_user refused" in refused.stdout + refused.stderr
    assert await _personal_count(url, user_id) == 2
    current = _alembic(url, "current")
    assert "o2p3q4r5s6t7" in current.stdout
