"""Settings for the harness: explicit values, and nothing from the developer's machine.

``Settings`` reads ``.env`` from the working directory and then the process
environment, so a plain ``Settings()`` in a test picks up the owner's real card
number, panel key and admin id. ``make_settings`` removes both before it builds
anything: the env file is switched off, and every variable the class declares is
dropped from the environment for the duration of the call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bot.config import Settings

ADMIN_IDS = [900_001, 900_002]
STORE_OWNER_IDS = [900_001]
BOT_TOKEN = "123456789:E2E-test-token-not-a-secret"
BOT_USERNAME = "neo_test_bot"
PANEL_URL = "https://panel.test"
PANEL_KEY = "pg_key_e2e_test_key"

CARD_NUMBER = "6037997711223344"
CARD_HOLDER = "علی رضایی"
CARD_BANK = "بانک تست"
SUPPORT_HANDLE = "@neo_support_test"
SUPPORT_HOURS = "۹ تا ۲۴"
CHANNEL_HANDLE = "@neo_channel_test"
ORDER_EXPIRY_MINUTES = 30


def _declared_env_names() -> set[str]:
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names.add((field.alias or name).upper())
        names.add(name.upper())
    return names


def make_settings(database_url: str, backup_dir: Path, **overrides: Any) -> Settings:
    """The harness's ``Settings``. ``overrides`` use the field names (``support_handle=...``)."""
    values: dict[str, Any] = {
        "BOT_TOKEN": BOT_TOKEN,
        "BOT_USERNAME": BOT_USERNAME,
        "ADMIN_TELEGRAM_IDS": list(ADMIN_IDS),
        "STORE_OWNER_TELEGRAM_IDS": list(STORE_OWNER_IDS),
        "SUPPORT_HANDLE": SUPPORT_HANDLE,
        "SUPPORT_HOURS_FA": SUPPORT_HOURS,
        "CHANNEL_HANDLE": CHANNEL_HANDLE,
        "RUN_MODE": "polling",
        "DATABASE_URL": database_url,
        "PASARGUARD_BASE_URL": PANEL_URL,
        "PASARGUARD_API_KEY": PANEL_KEY,
        "PASARGUARD_SUB_PATH": "sub",
        "PASARGUARD_DB_URL": None,
        "ORDER_EXPIRY_MINUTES": ORDER_EXPIRY_MINUTES,
        "PAYMENT_CARD_NUMBER": CARD_NUMBER,
        "PAYMENT_CARD_HOLDER_NAME": CARD_HOLDER,
        "PAYMENT_BANK_NAME": CARD_BANK,
        "PAYMENT_INSTRUCTIONS": "",
        # The scheduler object is stubbed by the harness (jobs run only when a
        # scenario calls ``app.run_job``), so enabling it here only makes sure
        # every job is registered and can be found by name.
        "SCHEDULER_ENABLED": True,
        "BACKUP_ENABLED": True,
        "BACKUP_DIR": str(backup_dir),
        "PANEL_WATCH_INTERVAL_SECONDS": 120,
        "SENTRY_DSN": "",
        "LOG_LEVEL": "WARNING",
        "DEBUG": False,
    }
    by_name = {n: f.alias or n for n, f in Settings.model_fields.items()}
    for name, value in overrides.items():
        if name not in by_name:
            raise TypeError(f"Settings has no field {name!r}")
        values[by_name[name]] = value

    declared = _declared_env_names()
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper() in declared}
    try:
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]
    finally:
        os.environ.update(saved)
