"""Admin access is shared and independent of the large admin router."""

from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace

from bot.config import Settings
from bot.handlers import admin


def test_admin_uses_the_shared_access_module_and_never_trusts_a_stale_db_flag() -> None:
    module_name = "bot.handlers.admin_access"
    assert importlib.util.find_spec(module_name) is not None, "admin access must be separated"
    access = importlib.import_module(module_name)
    assert admin._is_admin is access._is_admin
    settings = Settings(BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS="111")
    assert access._is_admin(111, settings, None)
    assert not access._is_admin(222, settings, SimpleNamespace(is_admin=True))
