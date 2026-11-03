"""The database: the real Alembic chain, migrated once, copied per test.

``Base.metadata.create_all`` always matches the models, which is exactly why it
cannot find a migration that left a CHECK or an index behind (that happened: the
migrated ``orders.status`` CHECK refused EXPIRED). So the schema comes from
``alembic upgrade head`` on a temporary SQLite *file*. The subprocess gets a
scrubbed environment and an env file switch, so ``alembic/env.py`` cannot read
the developer's ``.env`` or touch ``data/bot.db``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_RUNNER = """
from bot.config import Settings
Settings.model_config['env_file'] = None
from alembic.config import main
main(argv=['-c', 'alembic.ini', 'upgrade', 'head'])
"""


def build_template(directory: Path) -> Path:
    """Run every migration into ``directory/template.db`` and return its path."""
    path = directory / "template.db"
    if path.exists():
        path.unlink()
    url = f"sqlite+aiosqlite:///{path}"
    if "bot.db" in url:
        raise RuntimeError("refusing to migrate the customer database")
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(DATABASE_URL=url, BOT_TOKEN="123456789:template", SCHEDULER_ENABLED="false")
    result = subprocess.run(
        [sys.executable, "-c", _RUNNER],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")
    # Fold the write-ahead log into the file so a plain copy is a complete database.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    return path


def copy_template(template: Path, target: Path) -> str:
    """A private copy for one test; returns its async SQLAlchemy URL."""
    shutil.copyfile(template, target)
    return f"sqlite+aiosqlite:///{target}"
