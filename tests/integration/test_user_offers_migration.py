"""Disposable-database proof for ``z6c7d8e9f0a1`` (personal offers).

Never points Alembic at the customer database: ``DATABASE_URL`` is set only on
the child process, to a temporary SQLite file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.test_one_open_order_migration import _alembic

_PARENT = "z5b6c7d8e9f0"
_OFFERS = "z6c7d8e9f0a1"


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _seed_user(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_id, first_name, referral_code, is_admin, is_banned,"
            " is_reseller, wallet_balance_toman, language_code, created_at, updated_at) "
            "VALUES (1, 'A', 'MIG00001', 0, 0, 0, 0, 'fa', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        return int(conn.execute("SELECT id FROM users").fetchone()[0])
    finally:
        conn.close()


def _offer_sql(
    uid: int, *, event_key: str = "k1", percent: int = 10, order_id: str = "NULL"
) -> str:
    return (
        "INSERT INTO user_offers (user_id, campaign, percent, event_key, created_at,"
        f" expires_at, order_id) VALUES ({uid}, 'winback', {percent}, '{event_key}',"
        f" '2026-10-10', '2026-10-12', {order_id})"
    )


def test_the_offers_table_is_added_removed_and_added_again(tmp_path: Path) -> None:
    db = tmp_path / "offers.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    assert "user_offers" not in _tables(db)

    up = _alembic(url, "upgrade", _OFFERS)
    assert up.returncode == 0, up.stderr
    assert "user_offers" in _tables(db)

    down = _alembic(url, "downgrade", _PARENT)
    assert down.returncode == 0, down.stderr
    assert "user_offers" not in _tables(db)

    again = _alembic(url, "upgrade", _OFFERS)
    assert again.returncode == 0, again.stderr
    assert "user_offers" in _tables(db)


@pytest.mark.parametrize("percent", [0, 51, -1])
def test_a_percent_outside_one_to_fifty_is_refused(tmp_path: Path, percent: int) -> None:
    db = tmp_path / "range.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _OFFERS).returncode == 0
    uid = _seed_user(db)
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_offer_sql(uid, percent=percent))
    finally:
        conn.close()


def test_the_event_key_is_unique_and_so_is_the_order(tmp_path: Path) -> None:
    db = tmp_path / "unique.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _OFFERS).returncode == 0
    uid = _seed_user(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_offer_sql(uid, event_key="same"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_offer_sql(uid, event_key="same"))
        # Several offers with no order are fine: NULLs do not collide.
        conn.execute(_offer_sql(uid, event_key="a"))
        conn.execute(_offer_sql(uid, event_key="b"))
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM user_offers").fetchone()[0]
    finally:
        conn.close()
    assert count == 3
