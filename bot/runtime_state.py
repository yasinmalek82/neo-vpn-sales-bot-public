"""Standard-library reader for local runtime evidence, independent of the Telegram SDK."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

RUNTIME_HEALTH_PATH = Path("/tmp/neo-runtime-health.json")
MAX_AGE_SECONDS = 90


class RuntimeHealthError(RuntimeError):
    """Contains a fixed diagnostic code, never credentials or API error text."""


def check_runtime_health(path: Path = RUNTIME_HEALTH_PATH) -> dict[str, Any]:
    try:
        with path.open() as stream:
            state = json.loads(stream.read(4097))
    except (OSError, ValueError):
        raise RuntimeHealthError("runtime_evidence_invalid") from None
    if not isinstance(state, dict):
        raise RuntimeHealthError("runtime_evidence_invalid")
    pid = state.get("pid")
    mode = state.get("mode")
    loop_at: Any = state.get("loop_at")
    receive_at: Any = state.get("receive_at")
    if (
        type(pid) is not int
        or pid <= 0
        or mode not in ("polling", "webhook")
        or type(state.get("stopped")) is not bool
        or type(state.get("receiver_ok")) is not bool
        or type(loop_at) not in (int, float)
        or not math.isfinite(loop_at)
        or (
            mode == "polling"
            and state["receiver_ok"]
            and (type(receive_at) not in (int, float) or not math.isfinite(receive_at))
        )
    ):
        raise RuntimeHealthError("runtime_evidence_invalid")
    try:
        os.kill(pid, 0)
    except OSError:
        raise RuntimeHealthError("process_missing") from None
    if state["stopped"]:
        raise RuntimeHealthError("runtime_stopped")
    now = time.time()
    if not -5 <= now - loop_at <= MAX_AGE_SECONDS:
        raise RuntimeHealthError("event_loop_stale")
    if not state["receiver_ok"]:
        raise RuntimeHealthError("receiver_not_ready")
    if mode == "polling" and not -5 <= now - receive_at <= MAX_AGE_SECONDS:
        raise RuntimeHealthError("receiver_stale")
    return {"mode": mode, "receiver": "ready"}
