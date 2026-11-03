"""Local runtime evidence consumed by the container healthcheck.

Polling needs successful getUpdates, even when no messages arrive. Webhook mode
needs a started listener and a live event loop; external Telegram/TLS delivery
still requires the separate external smoke/monitoring route.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import Response, TelegramType

from bot.runtime_state import (
    MAX_AGE_SECONDS,
    RUNTIME_HEALTH_PATH,
    RuntimeHealthError,
    check_runtime_health,
)

__all__ = [
    "MAX_AGE_SECONDS",
    "RUNTIME_HEALTH_PATH",
    "ReceiveHealthMiddleware",
    "RuntimeHealth",
    "RuntimeHealthError",
    "check_runtime_health",
]

logger = logging.getLogger(__name__)


class ReceiveHealthMiddleware(BaseRequestMiddleware):
    def __init__(self, health: RuntimeHealth) -> None:
        self.health = health

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, cast("TelegramMethod[TelegramType]", method))
        except Exception:
            self.health.receive_failed()
            raise
        self.health.received()
        return result


class RuntimeHealth:
    def __init__(self, mode: str, *, path: Path = RUNTIME_HEALTH_PATH) -> None:
        if mode not in {"polling", "webhook"}:
            raise ValueError("invalid_runtime_mode")
        self.path = path
        self.state: dict[str, Any] = {
            "pid": os.getpid(),
            "mode": mode,
            "stopped": False,
            "loop_at": time.time(),
            "receive_at": None,
            "receiver_ok": False,
        }
        self.task: asyncio.Task[None] | None = None
        self.listener_probe: Callable[[], bool] | None = None

    def publish(self) -> None:
        self.state["loop_at"] = time.time()
        if self.listener_probe is not None:
            try:
                self.state["receiver_ok"] = bool(self.listener_probe())
            except (OSError, RuntimeError):
                self.state["receiver_ok"] = False
        # Replace atomically so another process never reads partial JSON.
        descriptor, name = tempfile.mkstemp(prefix=".neo-health-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.state, stream)
            Path(name).replace(self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _safe_publish(self) -> None:
        try:
            self.publish()
        except OSError:
            # Monitoring failure must never turn accepted updates into failed IO.
            # Old evidence ages out in the independent healthcheck.
            logger.error("Runtime health evidence write failed")  # noqa: TRY400 — no raw paths

    def received(self) -> None:
        self.state.update(receiver_ok=True, receive_at=time.time())
        self._safe_publish()

    def receive_failed(self) -> None:
        self.state["receiver_ok"] = False
        self._safe_publish()

    def listener_ready(self, probe: Callable[[], bool]) -> None:
        if self.state["mode"] != "webhook":
            raise ValueError("not_webhook_mode")
        self.listener_probe = probe
        self.state["receiver_ok"] = True
        self.publish()

    def stopped(self) -> None:
        self.state["stopped"] = True
        self._safe_publish()

    def middleware(self) -> ReceiveHealthMiddleware:
        return ReceiveHealthMiddleware(self)

    async def _tick(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                self.publish()
        except OSError:
            # Stop refreshing evidence: the independent healthcheck fails stale.
            logger.error("Runtime health evidence write failed")  # noqa: TRY400 — no raw paths

    def start(self, bot: Bot) -> None:
        self.publish()
        bot.session.middleware(self.middleware())
        self.task = asyncio.create_task(self._tick(), name="runtime-health")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.stopped()
