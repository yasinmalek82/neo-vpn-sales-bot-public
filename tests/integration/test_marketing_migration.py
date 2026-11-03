"""Disposable-database proof for ``z4a5b6c7d8e9`` and ``z5b6c7d8e9f0`` (marketing nudges).

Never points Alembic at the customer database: ``DATABASE_URL`` is set only on
the child process, to a temporary SQLite file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.test_one_open_order_migration import _alembic

_LIFECYCLE = "y2z3a4b5c6d7"
_SETTINGS = "z4a5b6c7d8e9"
_NULLABLE = "z5b6c7d8e9f0"


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _account_id_not_null(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        for row in conn.execute("PRAGMA table_info(service_notices)"):
            if row[1] == "account_id":
                return bool(row[3])
    finally:
        conn.close()
    raise AssertionError("service_notices.account_id is missing")


def _seed_user_and_account_notice(path: Path) -> int:
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
        account = conn.execute("SELECT id FROM pasarguard_accounts").fetchone()[0]
        conn.execute(
            "INSERT INTO service_notices (event_key, kind, account_id, user_id, created_at)"
            " VALUES ('volwarn:old', 'volume_warning', ?, ?, '2026-01-01')",
            (account, uid),
        )
        conn.commit()
        return int(uid)
    finally:
        conn.close()


def test_settings_table_is_added_and_removed(tmp_path: Path) -> None:
    db = tmp_path / "mkt.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _LIFECYCLE).returncode == 0
    assert "marketing_settings" not in _tables(db)

    up = _alembic(url, "upgrade", _SETTINGS)
    assert up.returncode == 0, up.stderr
    assert "marketing_settings" in _tables(db)

    down = _alembic(url, "downgrade", _LIFECYCLE)
    assert down.returncode == 0, down.stderr
    assert "marketing_settings" not in _tables(db)

    again = _alembic(url, "upgrade", _SETTINGS)
    assert again.returncode == 0, again.stderr


def test_a_new_row_is_every_campaign_off_with_the_default_offer(tmp_path: Path) -> None:
    db = tmp_path / "row.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _SETTINGS).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO marketing_settings (id) VALUES (1)")
        row = conn.execute(
            "SELECT cart_enabled_at, trial_convert_enabled_at, winback_enabled_at,"
            " upgrade_enabled_at, trial_convert_offer_percent, trial_convert_offer_hours,"
            " winback_offer_percent, winback_offer_hours, upgrade_offer_percent,"
            " upgrade_offer_hours FROM marketing_settings"
        ).fetchone()
    finally:
        conn.close()
    assert row == (None, None, None, None, 0, 48, 0, 48, 0, 48)


@pytest.mark.parametrize(
    "assignment",
    [
        "winback_offer_percent = 51",
        "trial_convert_offer_percent = -1",
        "upgrade_offer_hours = 0",
        "winback_offer_hours = 169",
    ],
)
def test_the_offer_columns_refuse_a_value_outside_their_range(
    tmp_path: Path, assignment: str
) -> None:
    db = tmp_path / "range.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _SETTINGS).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO marketing_settings (id) VALUES (1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE marketing_settings SET {assignment}")
    finally:
        conn.close()


def test_a_notice_may_have_no_account_and_existing_notices_keep_theirs(tmp_path: Path) -> None:
    db = tmp_path / "null.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _SETTINGS).returncode == 0
    uid = _seed_user_and_account_notice(db)
    assert _account_id_not_null(db)

    up = _alembic(url, "upgrade", _NULLABLE)
    assert up.returncode == 0, up.stderr
    assert not _account_id_not_null(db)

    conn = sqlite3.connect(db)
    try:
        kept = conn.execute(
            "SELECT account_id FROM service_notices WHERE event_key = 'volwarn:old'"
        ).fetchone()
        assert kept is not None and kept[0] is not None
        conn.execute(
            "INSERT INTO service_notices (event_key, kind, account_id, user_id, created_at)"
            " VALUES ('mkt:cart:1', 'mkt_cart', NULL, ?, '2026-10-10')",
            (uid,),
        )
        conn.commit()
    finally:
        conn.close()


def test_downgrade_works_without_orphan_notices_and_refuses_with_one(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    url = f"sqlite+aiosqlite:///{db}"
    assert _alembic(url, "upgrade", _NULLABLE).returncode == 0
    uid = _seed_user_and_account_notice(db)

    clean = _alembic(url, "downgrade", _SETTINGS)
    assert clean.returncode == 0, clean.stderr
    assert _account_id_not_null(db)

    assert _alembic(url, "upgrade", _NULLABLE).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO service_notices (event_key, kind, account_id, user_id, created_at)"
            " VALUES ('mkt:cart:1', 'mkt_cart', NULL, ?, '2026-10-10')",
            (uid,),
        )
        conn.commit()
    finally:
        conn.close()

    refused = _alembic(url, "downgrade", _SETTINGS)
    assert refused.returncode != 0
    assert "belong to no service" in refused.stdout + refused.stderr
    assert not _account_id_not_null(db)
