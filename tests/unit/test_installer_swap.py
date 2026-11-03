"""Opt-in swap creation preserves host state and rolls back only its own work."""

import os
import subprocess
from types import SimpleNamespace

import pytest


def setup(tmp_path, monkeypatch, *, swap_kib=0, fail="", fs="ext4", free=3 * 1024**3):
    from scripts.installer_swap import SwapManager

    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 930816 kB\nSwapTotal: {swap_kib} kB\n")
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=root / ext4 defaults 0 1\n")
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename Type Size Used Priority\n")
    path = tmp_path / "neo-test.swap"
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == "swapon" and fail != "swapon":
            swaps.write_text(f"Filename Type Size Used Priority\n{path} file 1048576 0 -2\n")
        if args[0] == "swapoff" and fail != "swapoff":
            swaps.write_text("Filename Type Size Used Priority\n")
        return subprocess.CompletedProcess(
            args, 1 if args[0] == fail else 0, stdout=fs if args[0] == "findmnt" else "", stderr=""
        )

    def allocate(fd, size):
        assert size == 1024**3
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        os.write(fd, b"allocated")

    manager = SwapManager(
        path=path,
        fstab=fstab,
        meminfo=meminfo,
        swaps=swaps,
        runner=runner,
        disk_usage=lambda _: SimpleNamespace(free=free),
        allocate=allocate,
    )
    return manager, calls, path, fstab


@pytest.mark.parametrize(
    "interactive,mode,answer",
    [(True, "ask", ""), (True, "ask", "n"), (False, "ask", "yes"), (True, "no", "yes")],
)
def test_swap_never_created_without_explicit_opt_in(
    tmp_path, monkeypatch, interactive, mode, answer
):
    manager, calls, path, fstab = setup(tmp_path, monkeypatch)
    messages = []
    manager.offer(
        mode=mode, interactive=interactive, prompt=lambda _: answer, output=messages.append
    )
    assert not path.exists()
    assert calls == []
    assert fstab.read_text() == "UUID=root / ext4 defaults 0 1\n"
    assert any("RAM" in message and "swap" in message for message in messages)


@pytest.mark.parametrize("mode,interactive", [("ask", True), ("yes", False)])
def test_confirmed_swap_is_private_active_persistent_and_idempotent(
    tmp_path, monkeypatch, mode, interactive
):
    manager, calls, path, fstab = setup(tmp_path, monkeypatch)
    manager.offer(mode=mode, interactive=interactive, prompt=lambda _: "yes", output=lambda _: None)
    assert path.stat().st_mode & 0o777 == 0o600
    assert [call[0] for call in calls] == ["findmnt", "mkswap", "swapon"]
    assert fstab.read_text().count(str(path)) == 1
    # Simulate the host's new SwapTotal after activation, then rerun.
    manager.meminfo.write_text("MemTotal: 930816 kB\nSwapTotal: 1048576 kB\n")
    manager.offer(mode="yes", interactive=False, prompt=lambda _: "yes", output=lambda _: None)
    assert fstab.read_text().count(str(path)) == 1
    assert [call[0] for call in calls].count("mkswap") == 1


def test_existing_swap_is_preserved_without_even_prompting(tmp_path, monkeypatch):
    manager, calls, path, fstab = setup(tmp_path, monkeypatch, swap_kib=512000)
    manager.offer(
        mode="yes",
        interactive=False,
        prompt=lambda _: pytest.fail("must not double existing swap"),
        output=lambda _: None,
    )
    assert calls == [] and not path.exists()


@pytest.mark.parametrize("fail", ["mkswap", "swapon", "fstab"])
def test_failed_creation_cleans_only_new_swap_and_preserves_fstab(tmp_path, monkeypatch, fail):
    from scripts.installer_swap import SwapError

    manager, calls, path, fstab = setup(tmp_path, monkeypatch, fail=fail)
    if fail == "fstab":

        def broken(*args):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(manager, "persist", broken)
    with pytest.raises(SwapError):
        manager.offer(mode="yes", interactive=False, prompt=lambda _: "", output=lambda _: None)
    assert not path.exists()
    assert fstab.read_text() == "UUID=root / ext4 defaults 0 1\n"
    if fail == "fstab":
        assert [call[0] for call in calls][-1] == "swapoff"


@pytest.mark.parametrize("kind", ["file", "symlink", "disk", "filesystem"])
def test_existing_paths_or_unsupported_host_are_not_modified(tmp_path, monkeypatch, kind):
    from scripts.installer_swap import SwapError

    manager, calls, path, fstab = setup(
        tmp_path,
        monkeypatch,
        free=100 if kind == "disk" else 3 * 1024**3,
        fs="btrfs" if kind == "filesystem" else "ext4",
    )
    sentinel = tmp_path / "keep"
    sentinel.write_text("keep")
    if kind == "file":
        path.write_text("existing")
    if kind == "symlink":
        path.symlink_to(sentinel)
    with pytest.raises(SwapError):
        manager.offer(mode="yes", interactive=False, prompt=lambda _: "", output=lambda _: None)
    assert sentinel.read_text() == "keep"
    if kind == "file":
        assert path.read_text() == "existing"
    if kind == "symlink":
        assert path.is_symlink()
    assert "mkswap" not in [call[0] for call in calls]


def test_explicit_private_mode_is_preserved_even_with_restrictive_umask(tmp_path, monkeypatch):
    manager, calls, path, fstab = setup(tmp_path, monkeypatch)
    before = os.umask(0o777)
    try:
        manager.offer(mode="yes", interactive=False, prompt=lambda _: "", output=lambda _: None)
    finally:
        os.umask(before)
    assert path.stat().st_mode & 0o777 == 0o600


def test_partial_fstab_registration_is_removed_on_failure(tmp_path, monkeypatch):
    from scripts.installer_swap import SwapError

    manager, calls, path, fstab = setup(tmp_path, monkeypatch)
    original = manager.persist

    def fail_after_write(remove=False):
        original(remove=remove)
        if not remove:
            raise OSError("failure after registration")

    monkeypatch.setattr(manager, "persist", fail_after_write)
    with pytest.raises(SwapError):
        manager.offer(mode="yes", interactive=False, prompt=lambda _: "", output=lambda _: None)
    assert fstab.read_text() == "UUID=root / ext4 defaults 0 1\n"
    assert not path.exists()


def test_failed_swapoff_never_unlinks_an_active_swap_file(tmp_path, monkeypatch):
    from scripts.installer_swap import SwapError

    manager, calls, path, fstab = setup(tmp_path, monkeypatch, fail="swapoff")

    def broken(*args):
        raise OSError("fstab is read-only")

    monkeypatch.setattr(manager, "persist", broken)
    with pytest.raises(SwapError, match="preserved"):
        manager.offer(mode="yes", interactive=False, prompt=lambda _: "", output=lambda _: None)
    assert path.exists()
    assert manager.active()


def test_zero_allocator_writes_real_bytes_without_sparse_holes(tmp_path):
    from scripts.installer_swap import allocate_fd

    path = tmp_path / "zeros"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        allocate_fd(fd, 8192)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"\0" * 8192
    assert path.stat().st_blocks * 512 >= 8192
