"""Double taps on a money confirm must not run two handlers at once."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import SimpleEventIsolation

import bot.main as bot_main


def test_the_dispatcher_is_built_with_event_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without it aiogram runs two quick taps of one user concurrently (SUSPECT-FLOW-1/3,
    SUSPECT-MONEY-3); SimpleEventIsolation makes the second wait for the first."""
    # The handler routers are module singletons that attach to one Dispatcher for
    # the life of the process, so the wiring is stubbed out; only the constructor
    # arguments are under test.
    monkeypatch.setattr(bot_main, "get_session_factory", object)
    monkeypatch.setattr(bot_main, "register_middlewares", lambda _dp: None)
    monkeypatch.setattr(bot_main, "register_handlers", lambda _dp: None)
    dp = bot_main.create_dispatcher()
    assert isinstance(dp.fsm.events_isolation, SimpleEventIsolation)
