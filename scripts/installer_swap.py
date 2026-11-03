"""Explicit, idempotent 1 GiB swap setup for an owner-run Ubuntu installer."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

SIZE = 1024**3


class SwapError(Exception):
    """Safe installer error; do not include subprocess stderr."""


def allocate_fd(fd: int, size: int) -> None:
    block = b"\0" * (1024 * 1024)
    remaining = size
    while remaining:
        written = os.write(fd, block[: min(len(block), remaining)])
        if not written:
            raise OSError("Swap allocation did not advance")
        remaining -= written
    os.fsync(fd)


class SwapManager:
    def __init__(
        self,
        *,
        path: Path = Path("/neo-test.swap"),
        fstab: Path = Path("/etc/fstab"),
        meminfo: Path = Path("/proc/meminfo"),
        swaps: Path = Path("/proc/swaps"),
        runner: Callable[..., Any] = subprocess.run,
        disk_usage: Callable[..., Any] = shutil.disk_usage,
        allocate: Callable[[int, int], None] = allocate_fd,
    ) -> None:
        self.path, self.fstab, self.meminfo, self.swaps = path, fstab, meminfo, swaps
        self.runner, self.disk_usage, self.allocate = runner, disk_usage, allocate
        self.line = f"{path} none swap sw 0 0 # neo-test-installer\n"

    def active(self) -> bool:
        return any(
            line.split() and line.split()[0] == str(self.path)
            for line in self.swaps.read_text().splitlines()[1:]
        )

    def offer(
        self,
        *,
        mode: str,
        interactive: bool,
        prompt: Callable[[str], str],
        output: Callable[[str], None],
    ) -> None:
        if mode not in {"ask", "yes", "no"}:
            raise SwapError("Invalid swap mode")
        try:
            memory = {
                line.split(":", 1)[0]: int(line.split()[1])
                for line in self.meminfo.read_text().splitlines()
                if ":" in line
            }
            output(
                f"RAM: {memory['MemTotal'] // 1024} MiB; existing swap: {memory['SwapTotal'] // 1024} MiB"
            )
            if memory["SwapTotal"] > 0 or self.active():
                output("Existing swap is preserved; no additional swap will be created.")
                return
            if mode == "no" or (mode == "ask" and not interactive):
                output(
                    "Swap was not requested; no swap changes made. Noninteractive opt-in: --swap-1g."
                )
                return
            if mode == "ask" and prompt(
                "Create 1 GiB persistent swap? This is a starting point, not a capacity guarantee. [y/N] "
            ).strip().lower() not in {"y", "yes"}:
                output("Swap declined; no swap changes made.")
                return
            self.create()
            output("Created 1 GiB swap with mode 0600 and a persistent fstab entry.")
        except SwapError:
            raise
        except (OSError, ValueError, KeyError):
            raise SwapError(
                "Could not inspect or configure swap; no existing swap was replaced"
            ) from None

    def persist(self, remove: bool = False) -> None:
        if self.fstab.is_symlink():
            raise SwapError("Refusing to replace a symlinked fstab")
        lock = self.fstab.parent / ".neo-swap-fstab.lock"
        with lock.open("a+b") as guard:
            lock.chmod(0o600)
            fcntl.flock(guard, fcntl.LOCK_EX)
            before = self.fstab.read_text()
            if remove:
                after = "".join(
                    line for line in before.splitlines(keepends=True) if line != self.line
                )
            else:
                if any(
                    line.split() and line.split()[0] == str(self.path)
                    for line in before.splitlines()
                    if not line.lstrip().startswith("#")
                ):
                    raise SwapError("An fstab entry for this path already exists")
                after = before + ("\n" if before and not before.endswith("\n") else "") + self.line
            if after == before:
                return
            metadata = self.fstab.stat()
            fd, name = tempfile.mkstemp(prefix=".neo-fstab-", dir=self.fstab.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    os.fchmod(output.fileno(), metadata.st_mode & 0o777)
                    os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
                    output.write(after)
                    output.flush()
                    os.fsync(output.fileno())
                Path(name).replace(self.fstab)
            finally:
                Path(name).unlink(missing_ok=True)

    def _checked(self, *args: str) -> str:
        result = self.runner(list(args), capture_output=True, text=True, check=False)
        if result.returncode:
            raise SwapError(f"{args[0]} failed")
        return str(result.stdout).strip()

    def create(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            raise SwapError("Dedicated swap path already exists; it was left unchanged")
        if self.fstab.is_symlink() or any(
            line.split() and line.split()[0] == str(self.path)
            for line in self.fstab.read_text().splitlines()
            if not line.lstrip().startswith("#")
        ):
            raise SwapError("Existing swap configuration must be reviewed manually")
        fs = self._checked("findmnt", "-n", "-o", "FSTYPE", "--target", str(self.path.parent))
        if fs not in {"ext4", "xfs"}:
            raise SwapError(
                "Swap file creation supports ext4/xfs only; existing state is unchanged"
            )
        if self.disk_usage(self.path.parent).free < SIZE + 256 * 1024**2:
            raise SwapError("Insufficient disk space for 1 GiB swap plus 256 MiB reserve")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        identity = os.fstat(fd)
        try:
            try:
                os.fchmod(fd, 0o600)
                self.allocate(fd, SIZE)
            finally:
                os.close(fd)
            for command in ("mkswap", "swapon"):
                self._checked(command, str(self.path))
            self.persist()
        except BaseException as exc:
            try:
                if self.line in self.fstab.read_text().splitlines(keepends=True):
                    self.persist(remove=True)
                if self.active():
                    self._checked("swapoff", str(self.path))
                current = self.path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    self.path.unlink()
            except (OSError, SwapError):
                raise SwapError(
                    "Swap setup failed; cleanup was incomplete, so the dedicated file was preserved for review"
                ) from None
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise SwapError("Swap setup failed; only this run's new swap was removed") from None
