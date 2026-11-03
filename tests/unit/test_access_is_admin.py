"""One admin check, three callers: the bot's two functions delegate to it.

The check used to be written twice with the same body; the Mini App panel
(`web/`) would have been a third. A list that disagrees between the bot and
the panel is a hole, so the bot's two functions must answer exactly as the
shared one does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import Settings
from bot.handlers import admin as admin_handlers
from bot.handlers import common
from services.access import is_admin

ENV_ADMIN = 111
NOT_LISTED = 222

CALLERS = [
    pytest.param(is_admin, id="services.access"),
    pytest.param(common.is_admin, id="handlers.common"),
    pytest.param(admin_handlers._is_admin, id="handlers.admin"),
]


def _settings() -> Settings:
    return Settings(BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS=str(ENV_ADMIN))


@pytest.mark.parametrize("check", CALLERS)
def test_an_env_listed_id_is_admin_without_a_row(check) -> None:
    assert check(ENV_ADMIN, _settings(), None) is True


@pytest.mark.parametrize("check", CALLERS)
def test_a_stale_db_flag_cannot_restore_a_removed_admin(check) -> None:
    flagged = SimpleNamespace(is_admin=True)
    assert check(NOT_LISTED, _settings(), flagged) is False


@pytest.mark.parametrize("check", CALLERS)
def test_neither_the_list_nor_the_flag_is_not_admin(check) -> None:
    plain = SimpleNamespace(is_admin=False)
    assert check(NOT_LISTED, _settings(), plain) is False
    assert check(NOT_LISTED, _settings(), None) is False
    assert check(None, _settings(), None) is False


@pytest.mark.parametrize("name", ["common", "admin"])
def test_the_bot_functions_delegate_to_the_shared_check(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a handler grows its own body again, this goes red."""
    module = common if name == "common" else admin_handlers
    func = module.is_admin if name == "common" else module._is_admin
    monkeypatch.setattr(module, "access_is_admin", lambda *_a, **_k: "shared")
    assert func(NOT_LISTED, _settings(), None) == "shared"
