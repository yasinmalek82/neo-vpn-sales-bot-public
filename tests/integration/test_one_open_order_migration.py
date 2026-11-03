"""Disposable-database proof for the one-open-order migration.

Never points Alembic at the customer database. ``DATABASE_URL`` is set only on
the child process, to a temporary SQLite file.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import column, func, insert, select, table
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Insert

from database.models import Order, OrderStatus, Plan
from database.models.plan import DataLimitResetStrategy

ROOT = Path(__file__).resolve().parents[2]
_PARENT = "j7k8l9m0n1o2"
_LEGACY_STATUS_CHECK = (
    "CONSTRAINT order_status CHECK (status IN ('pending', 'paid', 'activated', "
    "'expired', 'cancelled', 'refunded')), "
)


def _drop_legacy_sqlite_status_check(url: str) -> None:
    """Alembic's SQLite chain still checks the pre-phase-1 status names.

    A database that already stores ``PENDING_PAYMENT`` (create_all, or
    PostgreSQL) can hold the historical duplicates this migration must refuse.
    The parent revision on SQLite cannot, until that leftover check is out of
    the way. This only edits the temporary file.
    """
    path = url.removeprefix("sqlite+aiosqlite:///")
    connection = sqlite3.connect(path)
    try:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='orders'").fetchone()
        assert sql is not None
        updated = sql[0].replace(_LEGACY_STATUS_CHECK, "")
        assert updated != sql[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='orders'",
            (updated,),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    finally:
        connection.close()


def insert_order_row(**values: object) -> Insert:
    """INSERT into ``orders`` naming exactly the columns given.

    A database stopped at an older revision lacks later columns, and the live
    ``Order.__table__`` adds every column that has a Python default — so an
    insert through it breaks the day a defaulted column is added.
    """
    now = datetime.now(UTC)
    row = {"created_at": now, "updated_at": now, **values}
    return insert(table("orders", *(column(name) for name in row))).values(**row)


def insert_user_row(**values: object) -> Insert:
    """Seed exactly the historical user columns; never use today's ORM defaults."""
    now = datetime.now(UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "language_code": "fa",
        "wallet_balance_toman": 0,
        "is_admin": False,
        "is_reseller": False,
        "is_banned": False,
        **values,
    }
    return (
        insert(table("users", *(column(name) for name in row)))
        .values(**row)
        .returning(column("id"))
    )


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["BOT_TOKEN"] = "test-token"
    env["SCHEDULER_ENABLED"] = "false"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def _insert_two_open_orders(url: str) -> int:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user_id = int(
                (
                    await session.execute(
                        insert_user_row(
                            telegram_id=55,
                            username="buyer",
                            first_name="B",
                            referral_code="BUYER001",
                        )
                    )
                ).scalar_one()
            )
            plan = Plan(
                name="volume-10GB-30d",
                description="t",
                data_limit_gb=10,
                duration_days=30,
                price_toman=100_000,
                data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
                group_ids=[1],
                hwid_limit=1,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
            # This database is stopped at the parent revision, so the INSERT
            # names only columns that revision already has. The live Order
            # mapper also carries columns added later.
            for _ in range(2):
                await session.execute(
                    insert_order_row(
                        user_id=user_id,
                        plan_id=int(plan.id),
                        status=OrderStatus.PENDING_PAYMENT.value,
                        price_toman=100_000,
                        discount_toman=0,
                        wallet_applied_toman=0,
                        force_new=False,
                    )
                )
            await session.commit()
            return user_id
    finally:
        await engine.dispose()


async def _open_count(url: str, user_id: int) -> int:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == user_id,
                    Order.status.in_((OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW)),
                )
            )
            return int(count or 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_open_order_migration_round_trips(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    down = _alembic(url, "downgrade", "-1")
    assert down.returncode == 0, down.stderr
    again = _alembic(url, "upgrade", "head")
    assert again.returncode == 0, again.stderr


@pytest.mark.asyncio
async def test_one_open_order_migration_keeps_historical_duplicates(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'dupes.db'}"
    parent = _alembic(url, "upgrade", _PARENT)
    assert parent.returncode == 0, parent.stderr
    _drop_legacy_sqlite_status_check(url)
    user_id = await _insert_two_open_orders(url)
    refused = _alembic(url, "upgrade", "head")
    assert refused.returncode != 0
    combined = refused.stdout + refused.stderr
    assert "uq_orders_one_open_per_user refused" in combined
    assert "left in place" in combined
    assert await _open_count(url, user_id) == 2
