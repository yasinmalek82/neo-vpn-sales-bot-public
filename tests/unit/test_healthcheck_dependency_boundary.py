"""The real healthcheck reader must not load the Telegram producer SDK."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bot.runtime_health import RuntimeHealth

ROOT = Path(__file__).resolve().parents[2]
CHILD = """
import asyncio
import importlib.abc
import json
import os
import sys
from pathlib import Path

class NoTelegramSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'aiogram' or fullname.startswith('aiogram.'):
            raise ImportError('telegram_sdk_not_allowed')
        return None

sys.meta_path.insert(0, NoTelegramSDK())
from scripts.healthcheck import _check
result = {}
try:
    asyncio.run(_check(runtime_path=Path(os.environ['NEO_TEST_RUNTIME_PATH'])))
except Exception as exc:
    result['error_type'] = type(exc).__name__
else:
    result['checked'] = True
import database.base
result['closed'] = database.base._engine is None
result['sdk_absent'] = not any(name == 'aiogram' or name.startswith('aiogram.') for name in sys.modules)
print(json.dumps(result, sort_keys=True))
"""


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("healthy", {"checked": True, "closed": True, "sdk_absent": True}),
        (
            "receiver_not_ready",
            {"error_type": "RuntimeHealthError", "closed": True, "sdk_absent": True},
        ),
        ("bad_database", {"error_type": "OperationalError", "closed": True, "sdk_absent": True}),
    ],
)
def test_real_healthcheck_without_telegram_sdk(tmp_path, case, expected):
    """A child refuses every SDK import while consuming actual producer evidence."""
    health = RuntimeHealth("polling", path=tmp_path / "health.json")
    if case == "receiver_not_ready":
        health.publish()
    else:
        health.received()
    database = tmp_path / "synthetic.db"
    if case == "bad_database":
        database = tmp_path / "absent-directory" / "synthetic.db"
    else:
        sqlite3.connect(database).close()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NEO_DISABLE_DOTENV": "1",
        "BOT_TOKEN": "test-token",
        "SCHEDULER_ENABLED": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "NEO_TEST_RUNTIME_PATH": str(health.path),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    finally:
        health.stopped()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert not result.stderr
    if case == "bad_database":
        assert not database.parent.exists()
