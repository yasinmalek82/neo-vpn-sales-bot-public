"""The migrated SQLite schema must accept every value the Python enums hold.

Unit tests build their schema with ``Base.metadata.create_all``, which always
matches the models. Production SQLite is built by the Alembic chain, and there
``orders.status`` kept a CHECK without EXPIRED, so ``expire_stale_orders`` died
with ``CHECK constraint failed: order_status`` every five minutes. Only the
real migrations can catch that, so this runs them against a temporary file.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import sqlalchemy as sa

from database.base import Base
from database.models import OrderStatus
from tests.integration.test_one_open_order_migration import _alembic


def _migrate(tmp_path: Path) -> Path:
    path = tmp_path / "migrated.db"
    result = _alembic(f"sqlite+aiosqlite:///{path}", "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return path


def _table_sql(path: Path, table: str) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, table
    return str(row[0])


def test_migrated_orders_accepts_every_order_status(tmp_path: Path) -> None:
    path = _migrate(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        rejected = []
        for status in OrderStatus:
            try:
                connection.execute(
                    "INSERT INTO orders (user_id, plan_id, status, price_toman, "
                    "discount_toman, created_at, updated_at) "
                    "VALUES (1, 1, ?, 0, 0, '2026-01-01', '2026-01-01')",
                    (status.value,),
                )
            except sqlite3.IntegrityError:
                rejected.append(status.value)
            connection.rollback()
    finally:
        connection.close()
    assert rejected == [], f"migrated orders CHECK refuses {rejected}"


def test_every_migrated_enum_check_lists_every_python_value(tmp_path: Path) -> None:
    path = _migrate(tmp_path)
    missing: dict[str, list[str]] = {}
    guarded = 0
    for table in Base.metadata.sorted_tables:
        ddl = _table_sql(path, table.name)
        for column in table.columns:
            enum_type = column.type
            if not isinstance(enum_type, sa.Enum) or not enum_type.create_constraint:
                continue
            match = re.search(rf"CONSTRAINT {enum_type.name} CHECK \((.*?)\)[,\n]", ddl)
            if match is None:
                continue  # the migrated schema has no CHECK here; nothing to go stale
            guarded += 1
            lost = [v for v in enum_type.enums if f"'{v}'" not in match.group(1)]
            if lost:
                missing[f"{table.name}.{column.name}"] = lost
    assert guarded >= 1, "found no enum CHECK in the migrated schema; the test is blind"
    assert missing == {}, missing


_PARENT = "u8v9w0x1y2z3"
_LIVE_CHECK = (
    "CONSTRAINT order_status CHECK (status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW', "
    "'PAYMENT_APPROVED', 'PROVISIONING', 'ACTIVE', 'REJECTED', 'PROVISION_FAILED'))"
)


def _as_deployed_before_fix(path: Path) -> None:
    """Give ``orders`` the CHECK the live database carried: no EXPIRED."""
    ddl = _table_sql(path, "orders")
    match = re.search(r"CONSTRAINT order_status CHECK \(.*?\)\)", ddl)
    assert match is not None
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='orders'",
            (ddl.replace(match.group(0), _LIVE_CHECK),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    finally:
        connection.close()


def _seed_order(path: Path, status: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO orders (user_id, plan_id, status, price_toman, discount_toman, "
            "created_at, updated_at) VALUES (1, 1, ?, 0, 0, '2026-01-01', '2026-01-01')",
            (status,),
        )
        connection.commit()
    finally:
        connection.close()


def test_live_shaped_check_is_fixed_and_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    _as_deployed_before_fix(path)
    _seed_order(path, "PENDING_PAYMENT")

    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    ddl = _table_sql(path, "orders")
    assert "'EXPIRED'" in ddl
    assert "ck_orders_trial_kind" in ddl
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE orders SET status='EXPIRED'")
        connection.commit()
        names = {r[1] for r in connection.execute("PRAGMA index_list(orders)")}
        count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        connection.close()
    assert {"uq_orders_one_open_per_user", "uq_orders_personal_trial_user_group"} <= names
    assert count == 1

    refused = _alembic(url, "downgrade", _PARENT)
    assert refused.returncode != 0
    assert "EXPIRED" in refused.stderr
    assert "'EXPIRED'" in _table_sql(path, "orders")  # refused before touching the table

    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE orders SET status='PENDING_PAYMENT'")
        connection.commit()
    finally:
        connection.close()
    down = _alembic(url, "downgrade", _PARENT)
    assert down.returncode == 0, down.stderr
    assert "'EXPIRED'" not in _table_sql(path, "orders")
    assert _alembic(url, "upgrade", "head").returncode == 0
