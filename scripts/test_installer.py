#!/usr/bin/env python3
"""Install and operate the owner-approved offline neo-test bundle."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from scripts.installer_lifecycle import Lifecycle, LifecycleError
from scripts.installer_swap import SwapError, SwapManager


class InstallerError(Exception):
    """Safe-to-display installer failure."""


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict[str, Any]
    manifest_bytes: bytes
    images: dict[str, dict[str, str]]


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallerError("Bundle contains an unsafe file path")
    return path


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle(root: Path) -> Bundle:
    if root.is_symlink():
        raise InstallerError("Bundle directory cannot be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise InstallerError("Bundle directory is unavailable")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise InstallerError("Bundle contains a symlink")
        if not item.is_file() and not item.is_dir():
            raise InstallerError("Bundle contains an unsupported filesystem object")
    try:
        lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise InstallerError("Bundle metadata is missing or invalid") from None
    expected: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise InstallerError("Bundle checksum list is malformed")
        digest, name = match.groups()
        name = _safe_relative_path(name).as_posix()
        if name == "SHA256SUMS" or name in expected:
            raise InstallerError("Bundle checksum list is malformed")
        expected[name] = digest
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS"
    }
    if not expected or actual != set(expected):
        raise InstallerError("Bundle checksum list does not cover the payload")
    for name, digest in expected.items():
        path = root.joinpath(*PurePosixPath(name).parts)
        if path.is_symlink() or not path.is_file() or _digest_file(path) != digest:
            raise InstallerError("Bundle checksum verification failed")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise InstallerError("Unsupported bundle manifest")
    if manifest.get("platform") != "linux/amd64":
        raise InstallerError("Bundle platform is not linux/amd64")
    revision = manifest.get("source_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise InstallerError("Bundle source revision is invalid")
    if manifest.get("source_tag") != f"test/installer-{revision[:12]}":
        raise InstallerError("Bundle source tag does not match its revision")
    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != {"bot", "db", "caddy"}:
        raise InstallerError("Bundle image manifest is invalid")
    for name, image in images.items():
        if (
            not isinstance(image, dict)
            or not isinstance(image.get("ref"), str)
            or not image["ref"]
            or not isinstance(image.get("id"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"])
            or image.get("architecture") != "amd64"
        ):
            raise InstallerError(f"Bundle image metadata is invalid: {name}")
    try:
        compose = (root / "compose.yml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise InstallerError("Bundle compose file is missing or invalid") from None
    for variable in ("NEO_BOT_IMAGE", "NEO_DB_IMAGE", "NEO_CADDY_IMAGE"):
        if not re.search(r"\$\{" + variable + r"(?::[^}]*)?\}", compose):
            raise InstallerError("Bundle compose image references do not match its manifest")
    if any(
        not (root / name).exists()
        for name in ("images.tar.gz", "compose.yml", "install.sh", "source")
    ):
        raise InstallerError("Bundle is missing a required payload")
    return Bundle(root, manifest, raw, images)


def validate_admin_ids(value: str) -> str:
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not re.fullmatch(r"[0-9]+", part) for part in parts):
        raise ValueError("Enter one or more positive numeric Telegram IDs")
    nums = [int(part) for part in parts]
    if any(n < 1 or n > 9_223_372_036_854_775_807 for n in nums) or len(set(nums)) != len(nums):
        raise ValueError("IDs must be unique positive signed 64-bit integers")
    return ",".join(str(n) for n in nums)


def validate_https_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Enter an HTTPS panel origin without credentials, path, query or fragment")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("Panel URL port is invalid") from None
    return f"https://{parsed.netloc}".rstrip("/")


def quote_compose_env(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("$", "$$")


def _validate_secret(value: str, label: str) -> str:
    secret = value.strip()
    if not secret:
        raise ValueError(f"{label} is required")
    if "\n" in secret or "\r" in secret:
        raise ValueError(f"{label} cannot contain a newline")
    return secret


def _invalid(message: str) -> str:
    raise ValueError(message)


def _parse_probe(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if result.returncode:
        raise InstallerError("Credential verification failed; check the value and try again")
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise InstallerError("Credential verification returned an invalid response") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise InstallerError("Credential verification failed; check the value and try again")
    return data


def _run_process(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, check=False, **kwargs)
    except subprocess.TimeoutExpired:
        raise InstallerError("Connection check timed out; check connectivity and retry") from None
    except OSError:
        raise InstallerError("A required system command is unavailable") from None


def _default_host_check() -> None:
    if os.geteuid() != 0:
        raise InstallerError("Run the installer with sudo")
    if sys.platform != "linux":
        raise InstallerError("Installation requires Ubuntu Linux")
    try:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        raise InstallerError("Ubuntu 22.04 or 24.04 is required") from None
    if not re.search(r'^ID=(?:ubuntu|"ubuntu")$', release, re.M) or not re.search(
        r'^VERSION_ID="?(22\.04|24\.04)"?$', release, re.M
    ):
        raise InstallerError("Ubuntu 22.04 or 24.04 is required")
    if os.uname().machine not in {"x86_64", "amd64"}:
        raise InstallerError("Installation requires an amd64 host")


def _official_docker_install(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    which: Callable[[str], str | None] = shutil.which,
    os_release_file: Path = Path("/etc/os-release"),
    source_file: Path = Path("/etc/apt/sources.list.d/docker.list"),
) -> None:
    if which("docker"):
        if runner(["docker", "info"], capture_output=True).returncode:
            raise InstallerError("Docker is installed but its service is unavailable")
        if runner(["docker", "compose", "version"], capture_output=True).returncode:
            raise InstallerError("Docker Compose plugin is unavailable")
        return
    for cmd in (
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "ca-certificates", "curl", "gnupg"],
        ["install", "-m", "0755", "-d", "/etc/apt/keyrings"],
    ):
        if runner(cmd, capture_output=True).returncode:
            raise InstallerError("Official Docker repository preparation failed")
    if not which("curl") or not which("gpg"):
        raise InstallerError(
            "Official Docker repository preparation did not install curl and gnupg"
        )
    with tempfile.NamedTemporaryFile() as key:
        if runner(
            ["curl", "-fsSL", "https://download.docker.com/linux/ubuntu/gpg", "-o", key.name],
            capture_output=True,
        ).returncode:
            raise InstallerError("Official Docker signing key download failed")
        if runner(
            ["gpg", "--dearmor", "--yes", "-o", "/etc/apt/keyrings/docker.gpg", key.name],
            capture_output=True,
        ).returncode:
            raise InstallerError("Official Docker signing key installation failed")
    runner(["chmod", "a+r", "/etc/apt/keyrings/docker.gpg"], capture_output=True)
    try:
        codename = next(
            x.split("=", 1)[1].strip().strip('"')
            for x in os_release_file.read_text(encoding="utf-8").splitlines()
            if x.startswith("VERSION_CODENAME=")
        )
    except (OSError, StopIteration):
        raise InstallerError("Could not identify the Ubuntu release codename") from None
    source_file.write_text(
        f"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu {codename} stable\n",
        encoding="utf-8",
    )
    cmds = (
        ["apt-get", "update"],
        [
            "apt-get",
            "install",
            "-y",
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
        ],
        ["systemctl", "enable", "--now", "docker"],
    )
    for cmd in cmds:
        if runner(cmd, capture_output=True).returncode:
            raise InstallerError("Docker installation from the official repository failed")


def _host_preflight(runner: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    for port in (80, 443, 8080):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0" if port != 8080 else "127.0.0.1", port))
        except OSError:
            raise InstallerError(f"Required port {port} is already occupied") from None
        finally:
            sock.close()
    if shutil.which("docker"):
        res = runner(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=neo-test",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
        )
        if res.returncode:
            raise InstallerError("Could not inspect existing Docker services")
        if res.stdout.strip():
            raise InstallerError(
                "A neo-test Compose project already exists outside the selected install directory"
            )


class Installer:
    def __init__(
        self,
        bundle_dir: Path,
        install_dir: Path = Path("/opt/neo-test"),
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = _run_process,
        prompt: Callable[[str], str] = input,
        secret_prompt: Callable[[str], str] = getpass.getpass,
        output: Callable[[str], None] = print,
        host_check: Callable[[], None] = _default_host_check,
        preflight: Callable[[Bundle], None] | None = None,
        ensure_docker: Callable[[], None] | None = None,
        resolve: Callable[..., list[Any]] = socket.getaddrinfo,
        swap_mode: str = "ask",
    ) -> None:
        self.bundle_dir, self.install_dir = Path(bundle_dir), Path(install_dir)
        self.runner, self.prompt, self.secret_prompt, self.output = (
            runner,
            prompt,
            secret_prompt,
            output,
        )
        self.host_check = host_check
        self.preflight = preflight or (lambda _bundle: _host_preflight(self.runner))
        self.ensure_docker = ensure_docker or (lambda: _official_docker_install(self.runner))
        self.resolve = resolve
        self.swap_mode = swap_mode

    def run(self, action: str) -> None:
        if action in {"update", "uninstall"}:
            self.host_check()
            if not self._existing_state(None):
                raise InstallerError("No installed instance was found. Choose Install first.")
            try:
                manager = Lifecycle(self)
                if action == "update":
                    manager.update(load_bundle(self.bundle_dir))
                else:
                    manager.uninstall()
            except LifecycleError as exc:
                raise InstallerError(str(exc)) from None
            return
        if action == "install":
            self.output("[1/6] Verify package")
            bundle = load_bundle(self.bundle_dir)
            self.host_check()
            if self._existing_state(bundle):
                self._compose("ps", check=False)
                self.output(
                    "A matching neo-test installation already exists; use status, start, or stop."
                )
                return
            if self.install_dir.exists() or self.install_dir.is_symlink():
                raise InstallerError(
                    "Install directory already exists without matching install metadata; refusing to change it"
                )
            self.output("[2/6] Check server and Docker")
            self.preflight(bundle)
            if sys.platform == "linux":
                try:
                    SwapManager().offer(
                        mode=self.swap_mode,
                        interactive=sys.stdin.isatty(),
                        prompt=self.prompt,
                        output=self.output,
                    )
                except SwapError as exc:
                    raise InstallerError(str(exc)) from None
            self.ensure_docker()
            self.output("[3/6] Load verified offline images")
            self._load_images(bundle)
            self.output("[4/6] Configure bot, panel and Blu")
            values = self._collect_settings(bundle)
            self._verify_dns(values["WEB_PANEL_DOMAIN"], values["PUBLIC_IPV4"])
            values.pop("PUBLIC_IPV4")
            self.output("[5/6] Review configuration")
            self._summary(values)
            if not self._confirm("Save this configuration?", default=False):
                raise InstallerError("Setup cancelled before saving configuration")
            self._install(bundle, values)
            self.output("[6/6] Start services")
            if not self._confirm("Start the isolated neo-test services now?", default=False):
                self.output(f"Saved. Start later: sudo sh {self.install_dir}/install.sh start")
                return
            if not self._prepare_polling():
                return
            self._compose("up", "-d", "--pull", "never", "--wait", "--wait-timeout", "180")
            self._compose("ps", check=False)
            self.output(
                "Services were started. Verify the test bot manually in Telegram; container health does not prove Telegram polling."
            )
            return
        self.host_check()
        if not self._existing_state(None):
            raise InstallerError("No matching neo-test installation was found")
        if action == "status":
            self._compose("ps", check=False)
        elif action == "start":
            if not self._prepare_polling():
                return
            self._compose("up", "-d", "--pull", "never")
            self._compose("ps", check=False)
        elif action == "stop":
            self._compose("stop")
        else:
            raise InstallerError("Unknown action")

    def _existing_state(self, bundle: Bundle | None) -> bool:
        manifest, env = self.install_dir / "manifest.json", self.install_dir / ".env"
        if not self.install_dir.exists():
            return False
        if (
            manifest.is_symlink()
            or env.is_symlink()
            or (self.install_dir / "compose.yml").is_symlink()
            or (self.install_dir / "source").is_symlink()
            or not manifest.is_file()
            or not env.is_file()
            or not (self.install_dir / "compose.yml").is_file()
            or not (self.install_dir / "source").is_dir()
        ):
            raise InstallerError(
                "Install metadata is incomplete; refusing to overwrite existing state"
            )
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise InstallerError(
                "Installed manifest is invalid; refusing to overwrite existing state"
            ) from None
        if (
            not isinstance(old, dict)
            or old.get("schema_version") != 1
            or old.get("platform") != "linux/amd64"
        ):
            raise InstallerError("Installed manifest is invalid; refusing to operate")
        if bundle is not None and old != bundle.manifest:
            raise InstallerError(
                "A different version is already installed. Choose Update to preserve its settings and data; Install does not overwrite an existing instance."
            )
        return True

    def _ask(self, label: str, validator: Callable[[str], str], secret: bool = False) -> str:
        while True:
            value = (self.secret_prompt if secret else self.prompt)(label)
            try:
                return validator(value)
            except ValueError as exc:
                self.output(str(exc))

    def _confirm(self, label: str, *, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.prompt(f"{label} {suffix} ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes", "بله", "1", "۱"}:
                return True
            if answer in {"n", "no", "خیر", "0", "۰"}:
                return False
            if answer in {"q", "quit"}:
                raise InstallerError("Setup cancelled")
            self.output("Choose y / n; Enter uses the displayed default.")

    def _choose(self, label: str, choices: set[str]) -> str:
        while True:
            answer = self.prompt(label).strip()
            if answer in choices:
                return answer
            if answer.lower() in {"q", "quit"}:
                return "0"
            self.output("Choose one of the displayed numbers; 0 cancels.")

    def _checked_probe(
        self, bundle: Bundle, kind: str, payload: dict[str, str]
    ) -> dict[str, Any] | None:
        while True:
            try:
                return self._run_probe(bundle, kind, payload)
            except InstallerError as exc:
                self.output(str(exc))
                choice = self._choose(
                    "[1] Retry  [2] Change credentials  [0] Cancel: ", {"0", "1", "2"}
                )
                if choice == "0":
                    raise InstallerError("Credential setup cancelled") from None
                if choice == "2":
                    return None

    def _summary(self, values: dict[str, str]) -> None:
        self.output(f"Bot: @{values['BOT_USERNAME']} | Admins: {values['ADMIN_TELEGRAM_IDS']}")
        self.output(f"Panel: {values['PASARGUARD_BASE_URL']}")
        self.output(f"Web: {values['WEB_PANEL_URL']} | Mode: polling")
        enabled = values.get("BANK_SMS_ENABLED") == "true"
        self.output(f"Blu SMS: {'enabled' if enabled else 'disabled'}")
        if enabled:
            self.output(f"Destination card: **** **** **** {values['PAYMENT_CARD_NUMBER'][-4:]}")
        self.output(
            "Secrets are hidden. Use a dedicated test bot; stop any other polling instance."
        )
        self.output(
            "AWS Security Group: allow inbound TCP 80/443 and outbound HTTPS. Local checks do not verify it."
        )

    def _collect_settings(self, bundle: Bundle) -> dict[str, str]:
        while True:
            token = self._ask("Telegram bot token: ", lambda v: _validate_secret(v, "Token"), True)
            probe = self._checked_probe(bundle, "telegram", {"token": token})
            if probe is None:
                continue
            username = str(probe.get("username", "")).lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                raise InstallerError("Telegram did not return a valid bot username")
            self._ask(
                f"Type @{username} to confirm this bot: ",
                lambda v, expected=username: (
                    expected
                    if v.strip().lstrip("@") == expected
                    else _invalid("Bot username confirmation did not match")
                ),
            )
            if probe.get("has_webhook"):
                self.output(
                    "Webhook is active. It stays unchanged during setup; switching is offered only when services start."
                )
                choice = self._choose(
                    "[1] Continue setup  [2] Different token  [0] Cancel: ", {"0", "1", "2"}
                )
                if choice == "0":
                    raise InstallerError("Webhook setup cancelled; webhook was not changed")
                if choice == "2":
                    continue
            break
        admins = self._ask("Administrator Telegram ID(s), comma separated: ", validate_admin_ids)
        owners = self._ask(
            "Store owner Telegram ID(s), comma separated (Enter = admins): ",
            lambda v: validate_admin_ids(v or admins),
        )
        while True:
            panel_url = self._ask("PasarGuard HTTPS base URL: ", validate_https_url)
            panel_key = self._ask(
                "PasarGuard API key: ", lambda v: _validate_secret(v, "API key"), True
            )
            if (
                self._checked_probe(bundle, "panel", {"base_url": panel_url, "api_key": panel_key})
                is not None
            ):
                break
        domain = self._ask("Test panel domain: ", _validate_domain)
        public_ip = self._ask("Server public IPv4 address: ", _validate_ipv4)
        import secrets

        password = secrets.token_urlsafe(32)
        values = {
            "BOT_TOKEN": token,
            "BOT_USERNAME": username,
            "ADMIN_TELEGRAM_IDS": admins,
            "STORE_OWNER_TELEGRAM_IDS": owners,
            "RUN_MODE": "polling",
            "POLLING_DROP_PENDING_UPDATES": "false",
            "DB_HOST": "db",
            "DB_PORT": "5432",
            "DB_NAME": "neo_test",
            "DB_USER": "neo_test",
            "DB_PASSWORD": password,
            "POSTGRES_DB": "neo_test",
            "POSTGRES_USER": "neo_test",
            "POSTGRES_PASSWORD": password,
            "SCHEDULER_ENABLED": "true",
            "DATABASE_URL": f"postgresql+asyncpg://neo_test:{password}@db:5432/neo_test",
            "PASARGUARD_BASE_URL": panel_url,
            "PASARGUARD_API_KEY": panel_key,
            "WEB_PANEL_ENABLED": "true",
            "WEB_PANEL_URL": f"https://{domain}/panel/",
            "WEB_PANEL_DOMAIN": domain,
            "WEB_PANEL_PUBLIC_ENABLED": "false",
            "BACKUP_ENABLED": "false",
        }
        for key, image_name in {
            "NEO_BOT_IMAGE": "bot",
            "NEO_DB_IMAGE": "db",
            "NEO_CADDY_IMAGE": "caddy",
        }.items():
            values[key] = bundle.images[image_name]["ref"]
        values["BANK_SMS_ENABLED"] = "false"
        if self._confirm("Enable automatic Blu SMS confirmation?", default=True):
            card = self._ask("Destination Blu card number (16 digits): ", _validate_card)
            holder = self._ask(
                "Blu card holder name: ",
                lambda v: (
                    _validate_secret(v, "Card holder")
                    if len(v.strip()) <= 64
                    else _invalid("Card holder name is too long")
                ),
            )
            values.update(
                {
                    "BANK_SMS_ENABLED": "true",
                    "BANK_SMS_TOKEN": secrets.token_urlsafe(48),
                    "BANK_SMS_SENDER": "BLUE",
                    "BANK_SMS_ACCOUNT_KEY": "blu-main",
                    "BANK_SMS_URL": f"https://{domain}/bank-sms/blu",
                    "PAYMENT_CARD_NUMBER": card,
                    "PAYMENT_CARD_HOLDER_NAME": holder,
                    "PAYMENT_BANK_NAME": "بلو",
                }
            )
        values["PUBLIC_IPV4"] = public_ip
        return values

    def _run_probe(self, bundle: Bundle, kind: str, payload: dict[str, str]) -> dict[str, Any]:
        args = [
            "docker",
            "run",
            "--pull=never",
            "--rm",
            "-i",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--entrypoint",
            "python",
            bundle.images["bot"]["ref"],
            "-m",
            "scripts.install_probe",
            kind,
        ]
        return _parse_probe(
            self.runner(args, input=json.dumps(payload), capture_output=True, timeout=60)
        )

    def _compose_probe(
        self, kind: str, payload: dict[str, str], *, directory: Path | None = None
    ) -> dict[str, Any]:
        args = self._compose_command(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "bot",
            "-m",
            "scripts.install_probe",
            kind,
        )
        return _parse_probe(
            self.runner(
                args,
                cwd=directory or self.install_dir,
                input=json.dumps(payload),
                capture_output=True,
                timeout=60,
            )
        )

    def _prepare_polling(self) -> bool:
        probe = self._compose_probe("telegram-config", {})
        if not probe.get("has_webhook"):
            return True
        username = str(probe.get("username", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise InstallerError("Telegram did not return a valid bot username")
        self.output(
            f"@{username} has an active webhook. Switching now stops delivery to the old webhook service."
        )
        if not self._confirm(
            "Remove this webhook and start polling? Pending updates will be kept.", default=False
        ):
            self.output(
                "Start cancelled; webhook was not changed. Saved settings are ready for a later start."
            )
            return False
        result = self._compose_probe(
            "telegram-config-remove-webhook", {"expected_username": username}
        )
        if result.get("has_webhook") or result.get("username") != username:
            raise InstallerError("Webhook removal could not be verified; services were not started")
        self.output("[OK] Webhook removed; pending updates were kept.")
        return True

    def _load_images(self, bundle: Bundle) -> None:
        loaded = self.runner(
            ["docker", "load", "-i", str(bundle.root / "images.tar.gz")],
            capture_output=True,
        )
        if loaded.returncode:
            raise InstallerError("Offline Docker image loading failed")
        for name, image in bundle.images.items():
            result = self.runner(
                [
                    "docker",
                    "image",
                    "inspect",
                    image["ref"],
                    "--format",
                    "{{.Id}} {{.Architecture}}",
                ],
                capture_output=True,
            )
            if result.returncode or result.stdout.strip() != f"{image['id']} amd64":
                raise InstallerError(f"Loaded {name} image did not match the manifest")

    def _verify_dns(self, domain: str, expected: str) -> None:
        try:
            seen = {answer[4][0] for answer in self.resolve(domain, 443, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            seen = set()
        if any(":" in answer for answer in seen):
            raise InstallerError(
                "AAAA record detected; this IPv4 test installer requires removing AAAA before continuing"
            )
        if seen != {expected}:
            raise InstallerError("Domain DNS must resolve only to the entered public IPv4 address")

    def _install(self, bundle: Bundle, values: dict[str, str]) -> None:
        lock = self.install_dir.parent / ".neo-test-install.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.install_dir.exists() or self.install_dir.is_symlink():
                raise InstallerError(
                    "Install directory appeared during setup; refusing to overwrite it"
                )
            with tempfile.TemporaryDirectory(
                prefix=".neo-test-stage-", dir=self.install_dir.parent
            ) as temporary:
                stage = Path(temporary) / "install"
                stage.mkdir(mode=0o700)
                shutil.copytree(bundle.root / "source", stage / "source")
                shutil.copy2(bundle.root / "compose.yml", stage / "compose.yml")
                shutil.copy2(bundle.root / "install.sh", stage / "install.sh")
                shutil.copy2(bundle.root / "manifest.json", stage / "manifest.json")
                (stage / "instance.json").write_text(
                    json.dumps(
                        {"project": "neo-test", "install_dir": str(self.install_dir.absolute())}
                    )
                    + "\n"
                )
                self._write_env(values, directory=stage)
                if values.get("BANK_SMS_ENABLED") == "true":
                    config = stage / "shortcut-settings.json"
                    shortcut_fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(shortcut_fd, "w", encoding="utf-8") as stream:
                        json.dump(
                            {
                                "url": values["BANK_SMS_URL"],
                                "sender": values["BANK_SMS_SENDER"],
                                "token": values["BANK_SMS_TOKEN"],
                            },
                            stream,
                            ensure_ascii=False,
                            indent=2,
                        )
                self._compose("config", "--quiet", directory=stage)
                if self.install_dir.exists() or self.install_dir.is_symlink():
                    raise InstallerError(
                        "Install directory appeared during setup; refusing to overwrite it"
                    )
                stage.rename(self.install_dir)
            if values.get("BANK_SMS_ENABLED") == "true":
                self.output(
                    f"Private iPhone Shortcut settings: {self.install_dir / 'shortcut-settings.json'} (root-only). Do not share this file."
                )
        finally:
            os.close(fd)

    def _write_env(self, values: dict[str, str], *, directory: Path | None = None) -> None:
        directory = directory or self.install_dir
        content = "".join(
            f"{key}={quote_compose_env(value)}\n"
            for key, value in values.items()
            if key != "PUBLIC_IPV4"
        )
        fd, name = tempfile.mkstemp(prefix=".env.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            Path(name).replace(directory / ".env")
        except Exception:
            with suppress(OSError):
                Path(name).unlink()
            raise

    def _compose_command(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            "neo-test",
            "--env-file",
            ".env",
            "-f",
            "compose.yml",
            *args,
        ]

    def _compose(self, *args: str, check: bool = True, directory: Path | None = None) -> None:
        result = self.runner(
            self._compose_command(*args), cwd=directory or self.install_dir, capture_output=True
        )
        if check and result.returncode:
            raise InstallerError("Docker Compose operation failed; use status for current state")
        if args and args[0] == "ps" and result.stdout:
            self.output(result.stdout.rstrip())


def _validate_card(value: str) -> str:
    translated = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r"[\s-]", "", translated)
    if not re.fullmatch(r"[0-9]{16}", digits):
        raise ValueError("Enter exactly 16 card digits")
    return digits


def _validate_domain(value: str) -> str:
    domain = value.strip().rstrip(".").lower()
    if len(domain) > 253 or not domain or ":" in domain or "/" in domain:
        raise ValueError("Enter a valid public DNS name")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a public DNS name instead of an IP address")
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels
    ):
        raise ValueError("Enter a valid public DNS name")
    return domain


def _validate_ipv4(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        raise ValueError("Enter a valid public IPv4 address") from None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError("Enter a public IPv4 address")
    return str(ip)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or operate the isolated neo-test bundle")
    parser.add_argument("--bundle-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/neo-test"))
    swap = parser.add_mutually_exclusive_group()
    swap.add_argument(
        "--swap-1g",
        action="store_true",
        help="Explicitly allow 1 GiB swap creation when no swap exists",
    )
    swap.add_argument("--no-swap", action="store_true", help="Skip the interactive swap offer")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("install", "status", "start", "stop", "update", "uninstall"),
        default=None,
    )
    args = parser.parse_args(argv)
    try:
        mode = "yes" if args.swap_1g else "no" if args.no_swap else "ask"
        installer = Installer(args.bundle_dir, args.install_dir, swap_mode=mode)
        action = args.action
        if action is None:
            print("\n==================================================")
            print("  NEO | Isolated test installation")
            print("  Ubuntu 22.04 / 24.04 - amd64")
            print("==================================================")
            print("  1  Install a new instance")
            print("  2  Service status")
            print("  3  Start services")
            print("  4  Stop services (data is kept)")
            print("  5  Update (keep settings and data)")
            print("  6  Uninstall")
            print("  0  Exit")
            selected = installer._choose("Choose an action: ", {"0", "1", "2", "3", "4", "5", "6"})
            if selected == "0":
                return 0
            action = {
                "1": "install",
                "2": "status",
                "3": "start",
                "4": "stop",
                "5": "update",
                "6": "uninstall",
            }[selected]
        if (
            action == "update"
            and args.bundle_dir.absolute() == args.install_dir.absolute()
            and not (args.bundle_dir / "images.tar.gz").is_file()
        ):
            from scripts.installer_lifecycle import update_from_latest

            return update_from_latest(args.install_dir)
        installer.run(action)
    except (KeyboardInterrupt, EOFError):
        print("Installer: Setup cancelled; rerun to continue.", file=sys.stderr)
        return 130
    except (InstallerError, LifecycleError) as exc:
        print(f"Installer: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Installer: A file operation failed; check free disk space and permissions, then inspect service status before retrying.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
