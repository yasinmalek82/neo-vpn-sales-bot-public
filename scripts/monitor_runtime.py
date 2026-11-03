#!/usr/bin/env python3
"""Independent runtime observer. No network without --probe or --notify; no app imports."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

REASONS = frozenset(
    {
        "healthy",
        "unreachable",
        "inspect_failed",
        "malformed",
        "image_mismatch",
        "stopped",
        "unhealthy",
        "no_health",
    }
)
IMAGE_PATTERN = re.compile(r"sha256:[a-f0-9]{64}\Z", re.ASCII)
INSPECT_TEMPLATE = (
    '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
    '"status":{{json .State.Status}},"health":{{with (index .State "Health")}}'
    '{{json (index . "Status")}}{{else}}null{{end}}}'
)
SSH_TIMEOUT = 15
HTTP_TIMEOUT = 10
MAX_RESPONSE = 65536


class MonitorError(Exception):
    """Fixed operator-facing reason; never embed external error contents."""


@dataclass(frozen=True)
class SSHConfig:
    target: str
    container: str
    key: Path
    known_hosts: Path
    sudo: bool = False

    def command(self) -> list[str]:
        if (
            not re.fullmatch(
                r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}",
                self.target,
                re.ASCII,
            )
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.container, re.ASCII)
            or not self.key.is_absolute()
            or not self.key.is_file()
            or not self.known_hosts.is_absolute()
            or not self.known_hosts.is_file()
            or any(char.isspace() or ord(char) < 32 for char in str(self.known_hosts))
        ):
            raise MonitorError("configuration_invalid")
        remote = (["sudo", "-n"] if self.sudo else []) + [
            "docker",
            "inspect",
            "--type=container",
            "--format",
            INSPECT_TEMPLATE,
            "--",
            self.container,
        ]
        return [
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-n",
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "LogLevel=ERROR",
            "--",
            self.target,
            shlex.join(remote),
        ]


def assess(value: Any, expected_image: str) -> str:
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "image", "running", "status", "health"}
        or not isinstance(value["id"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["id"], re.ASCII) is None
        or not isinstance(value["image"], str)
        or IMAGE_PATTERN.fullmatch(value["image"]) is None
        or type(value["running"]) is not bool
        or not isinstance(value["status"], str)
        or value["status"]
        not in {"created", "restarting", "running", "removing", "paused", "exited", "dead"}
        or not (value["health"] is None or isinstance(value["health"], str))
        or value["health"] not in {None, "healthy", "unhealthy", "starting"}
    ):
        return "malformed"
    if value["image"] != expected_image:
        return "image_mismatch"
    if not value["running"] or value["status"] != "running":
        return "stopped"
    if value["health"] is None:
        return "no_health"
    return "healthy" if value["health"] == "healthy" else "unhealthy"


def observe_ssh(config: SSHConfig, expected_image: str) -> str:
    command = config.command()
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=SSH_TIMEOUT
        )
    except UnicodeError:
        return "malformed"
    except (OSError, subprocess.SubprocessError):
        return "unreachable"
    if result.returncode == 255:
        return "unreachable"
    if result.returncode:
        return "inspect_failed"
    try:
        return (
            assess(json.loads(result.stdout), expected_image)
            if len(result.stdout) <= MAX_RESPONSE
            else "malformed"
        )
    except (ValueError, TypeError):
        return "malformed"


def observe_fixture(path: Path, expected_image: str) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            value = handle.read(MAX_RESPONSE + 1)
        return (
            assess(json.loads(value), expected_image) if len(value) <= MAX_RESPONSE else "malformed"
        )
    except (OSError, ValueError, TypeError):
        return "malformed"


def _fresh_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_state": None,
        "last_reason": None,
        "next_id": 1,
        "pending": [],
        "route": None,
    }


def _valid_positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _validate_state(state: Any) -> None:
    valid = (
        isinstance(state, dict)
        and set(state) == set(_fresh_state())
        and type(state["version"]) is int
        and state["version"] == 1
        and state["last_state"] in {None, "healthy", "unhealthy"}
        and state["last_reason"] in REASONS | {None}
        and _valid_positive(state["next_id"])
        and state["next_id"] < 10**12
        and isinstance(state["pending"], list)
    )
    if not valid:
        raise MonitorError("state_invalid")
    expected_state = (
        None
        if state["last_reason"] is None
        else ("healthy" if state["last_reason"] == "healthy" else "unhealthy")
    )
    if state["last_state"] != expected_state:
        raise MonitorError("state_invalid")
    route = state["route"]
    if route is not None and (
        not isinstance(route, dict)
        or set(route) != {"bot_id", "chat_id"}
        or not all(_valid_positive(value) for value in route.values())
    ):
        raise MonitorError("state_invalid")
    previous = 0
    for event in state["pending"]:
        if (
            not isinstance(event, dict)
            or set(event) != {"id", "kind", "reason", "utc"}
            or not isinstance(event["id"], str)
            or re.fullmatch(r"neo-[0-9]{12}", event["id"], re.ASCII) is None
            or event["kind"] not in {"failure", "recovery"}
            or event["reason"] not in REASONS
            or (event["kind"] == "recovery") != (event["reason"] == "healthy")
            or not isinstance(event["utc"], str)
        ):
            raise MonitorError("state_invalid")
        number = int(event["id"][4:])
        if not previous < number < state["next_id"]:
            raise MonitorError("state_invalid")
        previous = number
        try:
            parsed = datetime.strptime(event["utc"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise MonitorError("state_invalid") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != event["utc"]:
            raise MonitorError("state_invalid")


def _read_state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise MonitorError("state_invalid")
    if not path.exists():
        return _fresh_state()
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        _validate_state(state)
    except (ValueError, TypeError) as exc:
        raise MonitorError("state_invalid") from exc
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".neo-monitor-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


@dataclass
class TelegramNotifier:
    token: str = field(repr=False)
    bot_id: int
    chat_id: int

    def __post_init__(self) -> None:
        if (
            not _valid_positive(self.bot_id)
            or not _valid_positive(self.chat_id)
            or re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", self.token, re.ASCII) is None
            or self.token.split(":", 1)[0] != str(self.bot_id)
        ):
            raise MonitorError("notification_configuration_invalid")

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Fixed host/method allowlist, no environment proxies, TLS verification, no redirects.
        if method not in {"getMe", "getChat", "sendMessage"}:
            raise MonitorError("notification_failed")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=urllib.parse.urlencode(payload).encode("ascii"),
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT) as response:
                data = response.read(MAX_RESPONSE + 1)
                if response.status != 200 or len(data) > MAX_RESPONSE:
                    raise MonitorError("notification_failed")
            body = json.loads(data)
            if (
                not isinstance(body, dict)
                or body.get("ok") is not True
                or not isinstance(body.get("result"), dict)
            ):
                raise MonitorError("notification_failed")
            return body["result"]
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise MonitorError("notification_failed") from exc

    def verify(self) -> None:
        me = self.call("getMe", {})
        if type(me.get("id")) is not int or me["id"] != self.bot_id or me.get("is_bot") is not True:
            raise MonitorError("notification_identity_mismatch")
        chat = self.call("getChat", {"chat_id": self.chat_id})
        if (
            type(chat.get("id")) is not int
            or chat["id"] != self.chat_id
            or chat.get("type") != "private"
        ):
            raise MonitorError("notification_identity_mismatch")

    def send(self, event: dict[str, str]) -> None:
        text = (
            f"NEO_RUNTIME event={event['kind']} reason={event['reason']} "
            f"id={event['id']} utc={event['utc']}"
        )
        message = self.call("sendMessage", {"chat_id": self.chat_id, "text": text})
        chat = message.get("chat")
        if (
            not _valid_positive(message.get("message_id"))
            or not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or chat["id"] != self.chat_id
            or chat.get("type") != "private"
        ):
            raise MonitorError("notification_not_confirmed")


def run_once(
    path: Path, observe: Callable[[], str], notifier: TelegramNotifier | None = None
) -> dict[str, Any]:
    with _state_lock(path):
        state = _read_state(path)
        if notifier is not None:
            route = {"bot_id": notifier.bot_id, "chat_id": notifier.chat_id}
            if state["route"] is not None and state["route"] != route:
                raise MonitorError("route_mismatch")
            state["route"] = route
        reason = observe()
        if reason not in REASONS:
            raise MonitorError("observation_invalid")
        current = "healthy" if reason == "healthy" else "unhealthy"
        if current != state["last_state"] and (
            current == "unhealthy" or state["last_state"] == "unhealthy"
        ):
            state["pending"].append(
                {
                    "id": f"neo-{state['next_id']:012d}",
                    "kind": "failure" if current == "unhealthy" else "recovery",
                    "reason": reason,
                    "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
            state["next_id"] += 1
        state.update(last_state=current, last_reason=reason)
        _save_state(path, state)
        delivered, notification = 0, "disabled"
        if notifier is not None and state["pending"]:
            try:
                notifier.verify()
                while state["pending"]:
                    notifier.send(state["pending"][0])
                    # Telegram acceptance before this durable ACK may duplicate this event on retry.
                    acknowledged = {**state, "pending": state["pending"][1:]}
                    _save_state(path, acknowledged)
                    state = acknowledged
                    delivered += 1
                notification = "confirmed"
            except (MonitorError, OSError, ValueError):
                notification = "failed"
        return {
            "healthy": current == "healthy",
            "reason": reason,
            "pending": len(state["pending"]),
            "delivered": delivered,
            "notification": notification,
        }


def _configured_observer(args: argparse.Namespace) -> Callable[[], str]:
    if (
        not args.expected_image
        or IMAGE_PATTERN.fullmatch(args.expected_image) is None
        or args.state is None
        or not args.state.is_absolute()
        or not (args.probe or args.fixture is not None)
    ):
        raise MonitorError("configuration_invalid")
    if args.probe:
        if not all((args.ssh_target, args.container, args.ssh_key, args.known_hosts)):
            raise MonitorError("configuration_invalid")
        config = SSHConfig(
            args.ssh_target, args.container, args.ssh_key, args.known_hosts, args.sudo
        )
        config.command()  # Validate before any state write.
        return partial(observe_ssh, config, args.expected_image)
    return partial(observe_fixture, args.fixture, args.expected_image)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="Explicit live read-only SSH probe")
    mode.add_argument("--fixture", type=Path, help="Offline JSON with narrow inspect schema")
    parser.add_argument("--expected-image")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--ssh-target")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--container")
    parser.add_argument(
        "--sudo", action="store_true", help="Explicit remote sudo -n docker inspect"
    )
    parser.add_argument(
        "--notify", action="store_true", help="Explicit Telegram delivery; env credentials only"
    )
    args = parser.parse_args(argv)
    if not args.probe and args.fixture is None and not args.notify:
        print(json.dumps({"probe": "not_requested"}))
        return 0
    try:
        observe = _configured_observer(args)
        notifier = None
        if args.notify:
            notifier = TelegramNotifier(
                os.environ.get("NEO_MONITOR_BOT_TOKEN", ""),
                int(os.environ.get("NEO_MONITOR_BOT_ID", "0")),
                int(os.environ.get("NEO_MONITOR_CHAT_ID", "0")),
            )
        result = run_once(args.state, observe, notifier)
        print(json.dumps(result, sort_keys=True))
        if result["notification"] == "failed":
            return 2
        return 0 if result["healthy"] else 1
    except MonitorError as exc:
        print(json.dumps({"error": str(exc), "healthy": False}))
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "monitor_failed", "healthy": False}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
