"""Disposable-database proof for ``y2z3a4b5c6d7`` (service lifecycle).

Never points Alembic at the customer database: ``DATABASE_URL`` is set only on
the child process, to a temporary SQLite file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.test_one_open_order_migration import _alembic

_PARENT = "y3z4a5b6c7d8"
_TARGET = "y2z3a4b5c6d7"


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _seed_account(path: Path, *, deleted: bool) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_id, first_name, referral_code, is_admin, is_banned,"
            " is_reseller, wallet_balance_toman, language_code, created_at, updated_at) "
            "VALUES (1, 'A', 'MIG00001', 0, 0, 0, 0, 'fa', '2026-01-01', '2026-01-01')"
        )
        uid = conn.execute("SELECT id FROM users").fetchone()[0]
        extra = ", deleted_at" if deleted else ""
        extra_v = ", '2026-02-01'" if deleted else ""
        conn.execute(
            "INSERT INTO pasarguard_accounts (user_id, panel_user_id, panel_username,"
            f" created_at, updated_at{extra}) VALUES (?, 5, 'mig_one', '2026-01-01',"
            f" '2026-01-01'{extra_v})",
            (uid,),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_adds_the_tables_and_columns_and_downgrade_removes_them(tmp_path: Path) -> None:
    db = tmp_path / "life.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    assert "lifecycle_settings" not in _tables(db)
    assert "deleted_at" not in _columns(db, "pasarguard_accounts")

    up = _alembic(url, "upgrade", _TARGET)
    assert up.returncode == 0, up.stderr
    assert {
        "lifecycle_settings",
        "service_notices",
        "lifecycle_batches",
        "lifecycle_batch_items",
    } <= _tables(db)
    assert {
        "limited_since",
        "deleted_at",
        "delete_requested_at",
        "delete_exempt",
    } <= _columns(db, "pasarguard_accounts")
    assert {"undelivered", "delivered_at"} <= _columns(db, "service_notices")

    down = _alembic(url, "downgrade", _PARENT)
    assert down.returncode == 0, down.stderr
    assert "lifecycle_settings" not in _tables(db)
    assert not {"service_notices", "lifecycle_batches", "lifecycle_batch_items"} & _tables(db)
    assert not {
        "limited_since",
        "deleted_at",
        "delete_requested_at",
        "delete_exempt",
    } & _columns(db, "pasarguard_accounts")

    again = _alembic(url, "upgrade", _TARGET)
    assert again.returncode == 0, again.stderr


def test_existing_accounts_survive_the_round_trip_and_start_live(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _PARENT).returncode == 0
    _seed_account_at_parent(db)
    assert _alembic(url, "upgrade", _TARGET).returncode == 0
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT panel_username, deleted_at, limited_since, delete_requested_at, delete_exempt"
            " FROM pasarguard_accounts"
        ).fetchone()
    finally:
        conn.close()
    # Every existing service starts live, not requested for deletion and not exempt.
    assert row == ("mig_one", None, None, None, 0)
    assert _alembic(url, "downgrade", _PARENT).returncode == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT panel_username FROM pasarguard_accounts").fetchone() == (
            "mig_one",
        )
    finally:
        conn.close()


def _seed_account_at_parent(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_id, first_name, referral_code, is_admin, is_banned,"
            " is_reseller, wallet_balance_toman, language_code, created_at, updated_at) "
            "VALUES (1, 'A', 'MIG00001', 0, 0, 0, 0, 'fa', '2026-01-01', '2026-01-01')"
        )
        uid = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO pasarguard_accounts (user_id, panel_user_id, panel_username,"
            " created_at, updated_at) VALUES (?, 5, 'mig_one', '2026-01-01', '2026-01-01')",
            (uid,),
        )
        conn.commit()
    finally:
        conn.close()


def test_downgrade_refuses_while_a_service_is_soft_deleted(tmp_path: Path) -> None:
    """Dropping ``deleted_at`` would bring back a service whose panel user is gone."""
    db = tmp_path / "deleted.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _TARGET).returncode == 0
    _seed_account(db, deleted=True)

    refused = _alembic(url, "downgrade", _PARENT)
    assert refused.returncode != 0
    assert "soft-deleted" in refused.stdout + refused.stderr
    assert "deleted_at" in _columns(db, "pasarguard_accounts")
    assert "lifecycle_settings" in _tables(db)


@pytest.mark.parametrize("mode", ["off", "rehearsal", "on"])
def test_the_settings_table_takes_only_a_known_mode(tmp_path: Path, mode: str) -> None:
    db = tmp_path / "mode.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _TARGET).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO lifecycle_settings (id, delete_mode) VALUES (1, ?)", (mode,))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO lifecycle_settings (id, delete_mode) VALUES (2, 'maybe')")
    finally:
        conn.close()
