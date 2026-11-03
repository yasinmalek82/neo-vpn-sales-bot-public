"""No typed text and no subscription token may reach a log line.

Admins type the panel API key and the payment card into the bot, resellers type
customers' phone numbers, and the panel URL ``/sub/<token>/info`` carries the
customer's token. Logs go to stdout and, in Docker, to an unrotated json file.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.types import Message

import bot.main  # noqa: F401  (configures logging as the real process does)
from bot.handlers.common import unknown_message
from bot.middlewares.logging import InteractionLoggingMiddleware


@pytest.fixture(autouse=True)
def _loggers_enabled() -> None:
    """alembic's ``fileConfig`` (``alembic/env.py``) disables every logger that already
    exists, and the migration tests run it earlier in the same process. A disabled logger
    emits nothing, so the "not in caplog.text" asserts below would pass without proving
    anything; each test therefore asserts a positive marker as well."""
    for name in ("bot.interactions", "bot.handlers.common", "httpx"):
        logging.getLogger(name).disabled = False


def _message(text: str) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": datetime(2026, 10, 3),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "A"},
            "text": text,
        }
    )


async def _run_middleware(text: str, caplog: pytest.LogCaptureFixture) -> None:
    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        return None

    with caplog.at_level(logging.INFO, logger="bot.interactions"):
        await InteractionLoggingMiddleware()(handler, _message(text), {"raw_state": "S:x"})


async def test_the_interaction_log_never_holds_the_typed_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _run_middleware("pg_key_SUPERSECRET", caplog)
    assert "SUPERSECRET" not in caplog.text
    assert "len=18" in caplog.text


async def test_the_interaction_log_keeps_the_command_but_not_its_argument(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _run_middleware("/start REF42", caplog)
    assert "cmd=/start" in caplog.text
    assert "REF42" not in caplog.text


async def test_the_unhandled_message_log_never_holds_the_typed_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    message = MagicMock()
    message.text = "6037123412341234"
    message.from_user.id = 42
    message.answer = AsyncMock()
    settings = MagicMock()
    settings.admin_telegram_ids = []

    with caplog.at_level(logging.INFO):
        await unknown_message(message, state, settings, None)

    assert "Unhandled message from 42 (len=16)" in caplog.text
    assert "6037123412341234" not in caplog.text


async def test_httpx_does_not_log_the_subscription_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://panel.example.com/sub/TOKEN123/info")
    assert "TOKEN123" not in caplog.text
    # The request really went through httpx, only its INFO line is gone.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
