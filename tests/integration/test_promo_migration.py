"""Disposable-database proof for the ``promo_redemptions`` revision.

Never points Alembic at the customer database: ``DATABASE_URL`` is set only on the child
process, to a temporary SQLite file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.test_one_open_order_migration import _alembic

_PARENT = "z6c7d8e9f0a1"
_PROMO = "z7d8e9f0a1b2"


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _seed(path: Path) -> int:
    """One user; returns its id."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_id, first_name, referral_code, is_admin, is_banned,"
            " is_reseller, wallet_balance_toman, language_code, created_at, updated_at) "
            "VALUES (1, 'A', 'MIG00001', 0, 0, 0, 0, 'fa', '2026-01-01', '2026-01-01')"
        )
        uid = int(conn.execute("SELECT id FROM users").fetchone()[0])
        conn.commit()
        return uid
    finally:
        conn.close()


def _insert_order(path: Path, uid: int, status: str = "PENDING_PAYMENT") -> int:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO orders (user_id, plan_id, status, price_toman, discount_toman,"
            " wallet_applied_toman, force_new, renew_resets, custom_purchase, created_at,"
            " updated_at) VALUES (?, 1, ?, 1, 0, 0, 0, 0, 0,"
            " '2026-10-10', '2026-10-10')",
            (uid, status),
        )
        conn.commit()
        return int(conn.execute("SELECT max(id) FROM orders").fetchone()[0])
    finally:
        conn.close()


def _redemption_sql(uid: int, order_id: int, code: str = "SAVE15") -> str:
    return (
        "INSERT INTO promo_redemptions (code, user_id, order_id, created_at) "
        f"VALUES ('{code}', {uid}, {order_id}, '2026-10-10')"
    )


def test_the_redemptions_table_is_added_removed_and_added_again(tmp_path: Path) -> None:
    db = tmp_path / "promo.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    assert "promo_redemptions" not in _tables(db)

    up = _alembic(url, "upgrade", _PROMO)
    assert up.returncode == 0, up.stderr
    assert "promo_redemptions" in _tables(db)

    down = _alembic(url, "downgrade", _PARENT)
    assert down.returncode == 0, down.stderr
    assert "promo_redemptions" not in _tables(db)

    again = _alembic(url, "upgrade", _PROMO)
    assert again.returncode == 0, again.stderr
    assert "promo_redemptions" in _tables(db)


def test_an_order_can_be_redeemed_against_only_once_and_a_code_many_times(tmp_path: Path) -> None:
    db = tmp_path / "unique.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PROMO).returncode == 0
    uid = _seed(db)
    first = _insert_order(db, uid)
    second = _insert_order(db, uid, "ACTIVE")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_redemption_sql(uid, first))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_redemption_sql(uid, first, code="OTHER"))
        conn.execute(_redemption_sql(uid, second))  # the same code, another order: fine
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM promo_redemptions").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_the_code_is_indexed(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PROMO).returncode == 0
    conn = sqlite3.connect(db)
    try:
        indexed = {
            col
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='promo_redemptions'"
            )
            for col in [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")]
        }
    finally:
        conn.close()
    assert {"code", "order_id"} <= indexed
