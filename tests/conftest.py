"""Shared pytest fixtures / env defaults."""

from __future__ import annotations

import os

# Settings requires BOT_TOKEN at import/construction time for referral hooks.
os.environ.setdefault("BOT_TOKEN", "test-token")


import pytest

pytest_plugins = ["tests.integration.durability"]


@pytest.fixture(autouse=True)
def _no_report_group_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """One-tap report-group setup pauses between its test messages (flood
    control). Dozens of scenarios run it; none of them is about the pause."""
    monkeypatch.setattr("services.report_group.SEND_GAP_SECONDS", 0)


@pytest.fixture(scope="session")
def registered_dispatcher():
    """One Dispatcher for the whole session.

    aiogram routers are module-level singletons and refuse to attach to a second
    Dispatcher, so every test that inspects registration order has to share this
    one rather than building its own.
    """
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.config import Settings
    from bot.handlers import register_handlers

    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = Settings(BOT_TOKEN="test-token")
    register_handlers(dp)
    return dp


@pytest.fixture(scope="session")
def router_names(registered_dispatcher) -> list[str]:
    return [r.name for r in registered_dispatcher.sub_routers]
