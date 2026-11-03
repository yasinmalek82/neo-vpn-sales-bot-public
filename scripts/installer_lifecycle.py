"""Managed updates and removal of one neo-test installation.

Never restores a database automatically: a failed new process may already have
committed migrations or payments. Backups remain private and outside the live
installation directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from scripts.test_installer import Bundle, Installer


class LifecycleError(Exception):
    """A safe operator-facing lifecycle failure."""


def matches_image_identity(record: dict[str, str], inspected: str) -> bool:
    return any(
        inspected.strip() == f"{identity} amd64"
        for key in ("id", "oci_manifest_id")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", identity := record.get(key, ""))
    )


_VOLUME_KEYS = {"app_data", "app_backups", "db_data", "caddy_data", "caddy_config"}
_TAR_WRITE = "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/volume',arcname='data'); t.close()"
_TAR_READ = "import sys,tarfile\nwith tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as t:\n for m in t:\n  if m.isfile():\n   f=t.extractfile(m)\n   while f.read(1048576): pass\n"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleError(message)


def _tree_bytes(root: Path) -> int:
    total = 0
    for parent, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(parent) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total


class Lifecycle:
    def __init__(self, installer: Installer) -> None:
        self.app = installer
        self.root = installer.install_dir

    def _run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = self.app.runner(args, **kwargs)
        if result.returncode:
            raise LifecycleError(
                "A lifecycle command failed. Secrets and command output were withheld."
            )
        return result

    def _compose(
        self, *args: str, directory: Path | None = None, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return self._run(self.app._compose_command(*args), cwd=directory or self.root, **kwargs)

    def _json(self, args: list[str], **kwargs: Any) -> Any:
        result = self._run(args, capture_output=True, timeout=60, **kwargs)
        try:
            return json.loads(result.stdout)
        except (TypeError, ValueError):
            raise LifecycleError("Docker returned invalid lifecycle metadata.") from None

    def _model(self, directory: Path | None = None) -> dict[str, Any]:
        model = self._json(
            self.app._compose_command("config", "--format", "json"), cwd=directory or self.root
        )
        if not isinstance(model, dict) or set(model.get("services", {})) != {"bot", "db", "caddy"}:
            raise LifecycleError("Only the standard managed neo-test Compose layout is supported.")
        if model.get("name") != "neo-test" or set(model.get("volumes", {})) != _VOLUME_KEYS:
            raise LifecycleError(
                "Compose project or volumes differ from the managed neo-test layout."
            )
        base = (directory or self.root).absolute()
        expected_mounts = {
            "bot": {
                "/app/data": ("volume", "app_data"),
                "/app/backups": ("volume", "app_backups"),
                "/app/config": ("bind", str(base / "source/config")),
            },
            "db": {"/var/lib/postgresql/data": ("volume", "db_data")},
            "caddy": {
                "/etc/caddy/Caddyfile": ("bind", str(base / "source/docker/Caddyfile")),
                "/data": ("volume", "caddy_data"),
                "/config": ("volume", "caddy_config"),
            },
        }
        for service, expected in expected_mounts.items():
            mounts = model["services"][service].get("volumes", [])
            if not isinstance(mounts, list) or len(mounts) != len(expected):
                raise LifecycleError("Custom service mounts require manual lifecycle handling.")
            for mount in mounts:
                target = mount.get("target")
                if target not in expected:
                    raise LifecycleError("Unexpected data mount; automatic changes were refused.")
                kind, source = expected[target]
                allowed = (
                    {source, model["volumes"][source].get("name")} if kind == "volume" else {source}
                )
                if (
                    mount.get("type") != kind
                    or mount.get("source") not in allowed
                    or (kind == "bind" and not mount.get("read_only"))
                ):
                    raise LifecycleError(
                        "Custom or writable configuration mounts require manual handling."
                    )
        return model

    def _state(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        endpoint = os.environ.get("DOCKER_HOST", "")
        if endpoint and not endpoint.startswith("unix://"):
            raise LifecycleError(
                "Lifecycle actions require the local Docker daemon, not a remote DOCKER_HOST."
            )
        contexts = self._json(["docker", "context", "inspect"])
        if (
            not isinstance(contexts, list)
            or len(contexts) != 1
            or not str(
                contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
            ).startswith("unix://")
        ):
            raise LifecycleError("Lifecycle actions require a local Unix-socket Docker context.")
        for name in ("manifest.json", "compose.yml", ".env", "install.sh", "source"):
            path = self.root / name
            if path.is_symlink() or not path.exists():
                raise LifecycleError(
                    "Installed state is incomplete or contains an unsupported link."
                )
        if self.root.is_symlink():
            raise LifecycleError("Installation directory cannot be a symlink.")
        try:
            manifest = json.loads((self.root / "manifest.json").read_text())
        except (OSError, ValueError):
            raise LifecycleError("Installed manifest cannot be read.") from None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("platform") != "linux/amd64"
        ):
            raise LifecycleError("Installed manifest is not supported.")
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_revision", ""))):
            raise LifecycleError("Installed source revision is invalid.")
        images = manifest.get("images")
        if (
            not isinstance(images, dict)
            or set(images) != {"bot", "db", "caddy"}
            or any(
                not isinstance(record, dict)
                or not isinstance(record.get("ref"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(record.get("id", "")))
                for record in images.values()
            )
        ):
            raise LifecycleError("Installed image metadata is invalid.")
        marker = self.root / "instance.json"
        if marker.is_symlink():
            raise LifecycleError("Installation marker cannot be a symlink.")
        if marker.exists():
            try:
                identity = json.loads(marker.read_text())
            except (ValueError, OSError):
                raise LifecycleError("Installation marker is invalid.") from None
            if (
                not isinstance(identity, dict)
                or identity.get("project") != "neo-test"
                or identity.get("install_dir") != str(self.root.absolute())
            ):
                raise LifecycleError(
                    "This directory is a copy or backup, not the active installation."
                )
        elif self.root.absolute() != Path("/opt/neo-test"):
            raise LifecycleError(
                "Legacy lifecycle management is supported at /opt/neo-test only; copied/custom legacy installations require manual adoption."
            )
        model = self._model()
        ids = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=neo-test",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            timeout=30,
        ).stdout.splitlines()
        if ids:
            containers = self._json(["docker", "inspect", *ids])
            for container in containers:
                labels = container.get("Config", {}).get("Labels") or {}
                if str(labels.get("com.docker.compose.oneoff", "")).lower() == "true":
                    continue
                service = labels.get("com.docker.compose.service")
                if service not in model["services"] or labels.get(
                    "com.docker.compose.project.working_dir"
                ) != str(self.root.absolute()):
                    raise LifecycleError(
                        "The neo-test containers belong to a different or custom installation."
                    )
                declared = set()
                for mount in model["services"][service]["volumes"]:
                    kind, source = mount["type"], mount["source"]
                    if kind == "volume" and source in model["volumes"]:
                        source = model["volumes"][source]["name"]
                    declared.add((kind, source, mount["target"]))
                attached = {
                    (
                        mount["Type"],
                        mount.get("Name") if mount["Type"] == "volume" else mount["Source"],
                        mount["Destination"],
                    )
                    for mount in container.get("Mounts", [])
                }
                if attached != declared:
                    raise LifecycleError(
                        "Container mounts differ from the saved configuration; automatic changes were refused."
                    )
        env = model["services"]["bot"].get("environment")
        if not isinstance(env, dict):
            raise LifecycleError("Installed environment cannot be resolved by Compose.")
        values = {str(key): str(value) for key, value in env.items() if value is not None}
        database = model["services"]["db"].get("environment", {})
        for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            if str(database.get(key, "")) != values.get(key):
                raise LifecycleError(
                    "Database environment differs from the saved bot configuration; automatic changes were refused."
                )
        url = urlsplit(values.get("DATABASE_URL", ""))
        if (
            url.scheme != "postgresql+asyncpg"
            or url.hostname != "db"
            or url.port not in {None, 5432}
            or url.query
            or url.fragment
            or unquote(url.path.lstrip("/")) != database.get("POSTGRES_DB")
            or unquote(url.username or "") != database.get("POSTGRES_USER")
            or not all(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", str(database.get(key, "")))
                for key in ("POSTGRES_DB", "POSTGRES_USER")
            )
        ):
            raise LifecycleError(
                "Only the managed local database is supported; an external/custom database requires manual handling."
            )
        for service in ("bot", "db", "caddy"):
            record = manifest.get("images", {}).get(service, {})
            if record.get("ref") != model["services"][service].get("image"):
                raise LifecycleError(
                    "Installed images differ from their manifest; automatic changes were refused."
                )
        return manifest, model, values

    def _volumes(self, model: dict[str, Any]) -> dict[str, str]:
        found: dict[str, str] = {}
        listing = self._run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, timeout=30
        )
        existing = set(listing.stdout.splitlines())
        for key, record in model["volumes"].items():
            name = record.get("name", "")
            if (
                record.get("external")
                or record.get("driver") not in {None, "local"}
                or record.get("driver_opts")
                or not re.fullmatch(r"neo-test[_-][A-Za-z0-9_.-]+", name)
            ):
                raise LifecycleError("External or custom-named volumes require manual handling.")
            if name not in existing:
                continue
            metadata = self._json(["docker", "volume", "inspect", name])
            if not isinstance(metadata, list) or len(metadata) != 1:
                raise LifecycleError("Volume metadata is invalid.")
            labels = metadata[0].get("Labels") or {}
            if metadata[0].get("Driver") != "local":
                raise LifecycleError("Only local managed Docker volumes are supported.")
            if (
                labels.get("com.docker.compose.project") != "neo-test"
                or labels.get("com.docker.compose.volume") != key
            ):
                raise LifecycleError(
                    "Volume ownership is not neo-test; automatic changes were refused."
                )
            found[key] = name
        return found

    def _image(self, manifest: dict[str, Any], service: str) -> str:
        record = manifest.get("images", {}).get(service, {})
        reference, identity = record.get("ref", ""), record.get("id", "")
        if not reference or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise LifecycleError("Installed image identity is invalid.")
        result = self._run(
            ["docker", "image", "inspect", reference, "--format", "{{.Id}} {{.Architecture}}"],
            capture_output=True,
            timeout=30,
        )
        if not matches_image_identity(record, result.stdout):
            raise LifecycleError("Installed image could not be verified for backup.")
        return reference

    def _running(self) -> set[str]:
        result = self._compose(
            "ps", "--status", "running", "--services", capture_output=True, timeout=30
        )
        return set(result.stdout.splitlines())

    def _check_archive_images(self, manifest: dict[str, Any], volumes: dict[str, str]) -> None:
        if "db_data" in volumes:
            self._image(manifest, "db")
        if any(key != "db_data" for key in volumes):
            self._image(manifest, "bot")

    def _volume_bytes(self, image: str, name: str) -> int:
        script = "import os,stat\nn=0\nfor root,ds,fs in os.walk('/volume',followlinks=False):\n for f in fs:\n  s=os.lstat(os.path.join(root,f))\n  if stat.S_ISREG(s.st_mode): n+=s.st_size\nprint(n)"
        result = self._run(
            [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0",
                "--mount",
                f"type=volume,source={name},target=/volume,readonly",
                "--entrypoint",
                "python",
                image,
                "-c",
                script,
            ],
            capture_output=True,
            timeout=120,
        )
        try:
            return int(result.stdout.strip())
        except (ValueError, TypeError):
            raise LifecycleError("Could not estimate private backup size.") from None

    def _backup(
        self,
        manifest: dict[str, Any],
        model: dict[str, Any],
        values: dict[str, str],
        volumes: dict[str, str],
    ) -> Path:
        parent = self.root.with_name(self.root.name + "-backups")
        if parent.is_symlink():
            raise LifecycleError("Backup directory cannot be a symlink.")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077:
            raise LifecycleError("Backup directory must be private and owned by the current user.")
        _require(
            shutil.disk_usage(parent).free >= 256 * 1024**2 + 2 * _tree_bytes(self.root),
            "Insufficient free disk space for a private backup.",
        )
        backup = parent / ("backup-" + uuid.uuid4().hex[:12])
        backup.mkdir(mode=0o700)
        self.app.output(f"Private backup: {backup}")
        # A copy exists even if a later dump or update step fails.
        shutil.copytree(self.root, backup / "installation", symlinks=True)
        state: dict[str, Any] = {
            "source_revision": manifest["source_revision"],
            "status": "preparing",
            "files": {},
        }
        (backup / "backup.json").write_text(json.dumps(state, indent=2))
        other = {key: name for key, name in volumes.items() if key != "db_data"}
        image = self._image(manifest, "bot") if other else ""
        data_bytes = sum(self._volume_bytes(image, name) for name in other.values())
        if "db_data" in volumes:
            database = model["services"]["db"].get("environment", {})
            url = urlsplit(values.get("DATABASE_URL", ""))
            if url.hostname != "db" or unquote(url.path.lstrip("/")) != database.get("POSTGRES_DB"):
                raise LifecycleError(
                    "Database URL differs from the managed database; refusing an incomplete backup."
                )
            self._compose(
                "up",
                "-d",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "120",
                "db",
                capture_output=True,
                timeout=180,
            )
            size = self._compose(
                "exec",
                "-T",
                "db",
                "sh",
                "-ec",
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -Atqc "select pg_database_size(current_database())"',
                capture_output=True,
                timeout=60,
            )
            try:
                data_bytes += int(size.stdout.strip())
            except (ValueError, TypeError):
                raise LifecycleError("Could not estimate database backup size.") from None
            _require(
                shutil.disk_usage(backup).free >= 256 * 1024**2 + 2 * data_bytes,
                "Insufficient free space for database and volume backups; update/deletion was not applied.",
            )
            dump = backup / "database.pgdump"
            try:
                fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    self._compose(
                        "exec",
                        "-T",
                        "db",
                        "sh",
                        "-ec",
                        'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom',
                        stdout=stream,
                        stderr=subprocess.PIPE,
                        timeout=300,
                    )
                _require(
                    bool(dump.stat().st_size),
                    "Database backup is empty; no update or deletion was allowed.",
                )
                # Render the entire archive to /dev/null, without restoring a database.
                with dump.open("rb") as stream:
                    self._compose(
                        "exec",
                        "-T",
                        "db",
                        "pg_restore",
                        "--file=/dev/null",
                        stdin=stream,
                        capture_output=True,
                        timeout=300,
                    )
            except BaseException:
                dump.unlink(missing_ok=True)
                raise
        else:
            self.app.output("No existing database volume; configuration will be preserved.")
        _require(
            shutil.disk_usage(backup).free
            >= 256 * 1024**2 + 2 * sum(self._volume_bytes(image, name) for name in other.values()),
            "Insufficient free space for volume backups; update/deletion was not applied.",
        )
        if other:
            for key, name in other.items():
                archive = backup / (key + ".tar.gz")
                try:
                    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    args = [
                        "docker",
                        "run",
                        "--pull=never",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0",
                        "--mount",
                        f"type=volume,source={name},target=/volume,readonly",
                        "--entrypoint",
                        "python",
                        image,
                        "-c",
                        _TAR_WRITE,
                    ]
                    with os.fdopen(fd, "wb") as stream:
                        self._run(args, stdout=stream, stderr=subprocess.PIPE, timeout=300)
                    with archive.open("rb") as stream:
                        self._run(
                            [
                                "docker",
                                "run",
                                "--pull=never",
                                "--rm",
                                "-i",
                                "--network",
                                "none",
                                "--read-only",
                                "--entrypoint",
                                "python",
                                image,
                                "-c",
                                _TAR_READ,
                            ],
                            stdin=stream,
                            capture_output=True,
                            timeout=300,
                        )
                except BaseException:
                    archive.unlink(missing_ok=True)
                    raise
        for path in backup.iterdir():
            if path.is_file() and path.name != "backup.json":
                with path.open("rb") as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                state["files"][path.name] = digest.hexdigest()
        for path in backup.iterdir():
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        state["status"] = "ready"
        (backup / "backup.json").write_text(json.dumps(state, indent=2))
        return backup

    def _lock(self) -> int:
        path = self.root.parent / ".neo-test-install.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def update(self, bundle: Bundle) -> None:
        fd = self._lock()
        stopped = False
        try:
            old, model, values = self._state()
            if (
                old["source_revision"] == bundle.manifest["source_revision"]
                and old.get("images", {}).get("bot", {}).get("id") == bundle.images["bot"]["id"]
            ):
                self.app.output(
                    "This application version is already installed. Use Start if services are stopped."
                )
                return
            volumes = self._volumes(model)
            running = self._running()
            self._check_archive_images(old, volumes)
            self.app.output(
                f"Update: {old['source_revision'][:12]} -> {bundle.manifest['source_revision'][:12]}"
            )
            self.app.output(
                "Existing bot, panel, card, SMS token and data will be preserved. Bot/Caddy stop during backup and update; database migrations may run at startup."
            )
            if not self.app._confirm("Apply this update with a private backup?", default=False):
                return
            start = ("bot" in running) or self.app._confirm(
                "Start updated services after the update?", default=False
            )
            self.app._load_images(bundle)
            # Keep PostgreSQL and Caddy pinned; a bot update is not a database-engine upgrade.
            updated = dict(values)
            updated["NEO_BOT_IMAGE"] = bundle.images["bot"]["ref"]
            updated.setdefault("POLLING_DROP_PENDING_UPDATES", "false")
            actual_manifest = dict(bundle.manifest)
            actual_manifest["images"] = {
                "bot": bundle.images["bot"],
                "db": old["images"]["db"],
                "caddy": old["images"]["caddy"],
            }
            with tempfile.TemporaryDirectory(
                prefix=".neo-update-", dir=self.root.parent
            ) as temporary:
                stage = Path(temporary) / "candidate"
                shutil.copytree(self.root, stage, symlinks=True)
                config = self.root / "source/config"
                _require(
                    not config.is_symlink()
                    and not any(path.is_symlink() for path in config.rglob("*")),
                    "Linked configuration files require manual updating.",
                )
                shutil.rmtree(stage / "source")
                shutil.copytree(bundle.root / "source", stage / "source")
                if config.is_dir():
                    shutil.rmtree(stage / "source/config")
                    shutil.copytree(config, stage / "source/config")
                shutil.copy2(bundle.root / "compose.yml", stage / "compose.yml")
                shutil.copy2(bundle.root / "install.sh", stage / "install.sh")
                (stage / "manifest.json").write_text(json.dumps(actual_manifest, indent=2) + "\n")
                (stage / "instance.json").write_text(
                    json.dumps({"project": "neo-test", "install_dir": str(self.root.absolute())})
                    + "\n"
                )
                self.app._write_env(updated, directory=stage)
                if updated.get("BANK_SMS_ENABLED", "").lower() in {"true", "1", "yes", "on"}:
                    _require(
                        all(
                            updated.get(key)
                            for key in ("BANK_SMS_URL", "BANK_SMS_SENDER", "BANK_SMS_TOKEN")
                        ),
                        "Saved Blu SMS settings are incomplete; update was not applied.",
                    )
                    settings = stage / "shortcut-settings.json"
                    _require(not settings.is_symlink(), "Shortcut settings cannot be a symlink.")
                    secret_fd = os.open(
                        settings, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
                    )
                    with os.fdopen(secret_fd, "w") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        json.dump(
                            {
                                "url": updated["BANK_SMS_URL"],
                                "sender": updated["BANK_SMS_SENDER"],
                                "token": updated["BANK_SMS_TOKEN"],
                            },
                            stream,
                            indent=2,
                        )
                self.app._compose("config", "--quiet", directory=stage)
                self.app._compose_probe("configuration", {}, directory=stage)
                self._compose("stop", "bot", "caddy", capture_output=True, timeout=90)
                stopped = True
                volumes = self._volumes(model)
                backup = self._backup(old, model, values, volumes)
                _require(
                    shutil.disk_usage(self.root.parent).free >= 256 * 1024**2,
                    f"Low disk space after backup. Old files remain installed. Backup: {backup}",
                )
                previous = Path(temporary) / "previous"
                self.root.rename(previous)
                try:
                    stage.rename(self.root)
                except BaseException:
                    previous.rename(self.root)
                    raise
                self.app.output(
                    f"Update installed; settings and SMS key preserved. Backup: {backup}"
                )
                if start:
                    try:
                        if not self.app._prepare_polling():
                            return
                        self.app._compose(
                            "up", "-d", "--pull", "never", "--wait", "--wait-timeout", "180"
                        )
                        self.app._compose("ps", check=False)
                    except Exception:
                        try:
                            self.app._compose("stop", "bot", check=False)
                        except Exception:
                            self.app.output(
                                "Could not confirm that the bot stopped; inspect service status before recovery."
                            )
                        raise LifecycleError(
                            f"New files are installed but startup failed. Inspect service status before retrying. Database was not auto-restored. Backup: {backup}."
                        ) from None
                elif "db" not in running:
                    self._compose("stop", "db", capture_output=True, timeout=60)
        except (Exception, KeyboardInterrupt):
            if stopped:
                self.app.output(
                    "Update did not finish normally. Services may remain stopped; use Status. Data was not auto-restored."
                )
            raise
        finally:
            os.close(fd)

    def uninstall(self) -> None:
        fd = self._lock()
        try:
            manifest, model, values = self._state()
            self.app.output(
                "Uninstall neo-test only. Docker, swap, images and download cache are retained."
            )
            self.app.output("[1] Remove containers; keep all data and configuration")
            self.app.output("[2] Remove containers and active data; keep a private backup")
            self.app.output("[0] Cancel")
            choice = self.app._choose("Choose removal mode: ", {"0", "1", "2"})
            if choice == "0":
                return
            if choice == "1":
                if self.app._confirm("Remove application containers and keep data?", default=False):
                    self.app._compose("down", "--remove-orphans")
                    self.app.output(
                        "Containers removed. Data and settings retained. Use Start to run this version again, or Update for a new version."
                    )
                return
            volumes = self._volumes(model)
            self._check_archive_images(manifest, volumes)
            self.app.output(
                "This removes active wallet, order, payment and SMS-reservation history. Reusing the same Blu account with a fresh database requires reconciling pending transfers first."
            )
            self.app.output("Active volumes to remove: " + ", ".join(sorted(volumes.values())))
            if (
                self.app.prompt("Type DELETE neo-test DATA to confirm: ").strip()
                != "DELETE neo-test DATA"
            ):
                self.app.output("Removal cancelled.")
                return
            self._compose("stop", "bot", "caddy", capture_output=True, timeout=90)
            backup = self._backup(manifest, model, values, volumes)
            if self._volumes(model) != volumes:
                raise LifecycleError(
                    f"The volume set changed during backup. No data was removed. Backup: {backup}. Review Status and retry."
                )
            self.app._compose("down", "--remove-orphans")
            # Never force-remove volumes, remove shared images, or prune Docker.
            if volumes:
                self._run(
                    ["docker", "volume", "rm", *sorted(volumes.values())],
                    capture_output=True,
                    timeout=90,
                )
            # Keep the active configuration in the private backup as well.
            retired = backup / "removed-installation"
            self.root.rename(retired)
            self.app.output(
                f"Application and active data removed. Private backup retained: {backup}"
            )
        finally:
            os.close(fd)


def update_from_latest(install_dir: Path) -> int:
    """The installed controller can fetch a new approved release later."""
    from urllib.request import urlopen

    try:
        with urlopen(
            "https://raw.githubusercontent.com/yasinmalek82/neo-vpn-sales-bot-public/main/setup.sh",
            timeout=60,
        ) as response:
            script = response.read(65537)
        if len(script) > 65536:
            raise LifecycleError("Update launcher is too large.")
        return subprocess.run(
            ["/bin/sh", "-s", "--", "--install-dir", str(install_dir), "update"],
            input=script.decode("utf-8"),
            text=True,
            check=False,
        ).returncode
    except (OSError, ValueError):
        raise LifecycleError(
            "Could not download the approved update launcher; installed state was left in place."
        ) from None
