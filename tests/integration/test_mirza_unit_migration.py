"""An older imported batch retains records and an explicitly unknown currency."""

import json
import sqlite3
from pathlib import Path

from tests.integration.test_one_open_order_migration import _alembic


def test_source_unit_upgrade_and_downgrade_keep_existing_history(tmp_path: Path):
    db = tmp_path / "legacy-unit.db"
    url = f"sqlite+aiosqlite:///{db}"
    parent = _alembic(url, "upgrade", "zb1c2d3e4f5a")
    assert parent.returncode == 0, parent.stderr
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO mirza_import_batches(namespace,source_sha256,plan_sha256,created_at) VALUES (?,?,?,?)",
            ("legacy", "a" * 64, "b" * 64, "2026-01-01"),
        )
        con.execute(
            "INSERT INTO mirza_import_records(namespace,source_table,source_key,source_row_sha256,payload,disposition) VALUES (?,?,?,?,?,?)",
            (
                "legacy",
                "Payment_report",
                "p-1",
                "c" * 64,
                json.dumps({"price": "10000"}),
                "quarantined",
            ),
        )
    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT source_unit FROM mirza_import_batches").fetchone() == (None,)
        assert con.execute(
            "SELECT source_key,disposition FROM mirza_import_records"
        ).fetchall() == [("p-1", "quarantined")]
        con.execute("UPDATE mirza_import_batches SET source_unit='rial'")
    down = _alembic(url, "downgrade", "zb1c2d3e4f5a")
    assert down.returncode == 0, down.stderr
    again = _alembic(url, "upgrade", "head")
    assert again.returncode == 0, again.stderr
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT source_unit FROM mirza_import_batches").fetchone() == (None,)
        assert con.execute("SELECT count(*) FROM mirza_import_records").fetchone() == (1,)
