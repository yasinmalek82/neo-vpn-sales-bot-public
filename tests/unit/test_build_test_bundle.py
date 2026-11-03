"""The owner receives the committed source and verified images, never local secrets."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_bundle_builder_explains_its_cli_without_building() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_test_bundle.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--output-dir" in result.stdout


def _repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("print('committed')\n")
    (path / ".env.smoke.template").write_text("BOT_TOKEN=PASTE_BOTFATHER_TOKEN_HERE\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=path,
        check=True,
    )


def test_snapshot_uses_committed_bytes_and_excludes_untracked_credentials(
    tmp_path, monkeypatch
) -> None:
    from scripts import build_test_bundle as builder

    repo = tmp_path / "repo"
    _repository(repo)
    (repo / "app.py").write_text("uncommitted change\n")
    (repo / ".env").write_text("BOT_TOKEN=private\n")
    (repo / "ssh.pem").write_text("private key\n")
    monkeypatch.setattr(builder, "ROOT", repo)
    destination = tmp_path / "snapshot"
    revision = builder.source_snapshot("HEAD", destination)
    assert len(revision) == 40
    assert (destination / "app.py").read_text() == "print('committed')\n"
    assert (destination / ".env.smoke.template").read_text() == (
        "BOT_TOKEN=PASTE_BOTFATHER_TOKEN_HERE\n"
    )
    assert not (destination / ".env").exists()
    assert not (destination / "ssh.pem").exists()


def test_snapshot_refuses_a_tracked_secret_instead_of_shipping_it(tmp_path, monkeypatch) -> None:
    from scripts import build_test_bundle as builder

    repo = tmp_path / "repo"
    _repository(repo)
    (repo / ".env").write_text("BOT_TOKEN=private\n")
    subprocess.run(["git", "add", ".env"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "secret",
        ],
        cwd=repo,
        check=True,
    )
    monkeypatch.setattr(builder, "ROOT", repo)
    with pytest.raises(ValueError, match="forbidden"):
        builder.source_snapshot("HEAD", tmp_path / "snapshot")


def test_checksums_cover_the_payload_with_relative_paths(tmp_path) -> None:
    from scripts import build_test_bundle as builder

    (tmp_path / "hello.txt").write_text("hello")
    builder.write_checksums(tmp_path)
    assert (tmp_path / "SHA256SUMS").read_text() == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824  hello.txt\n"
    )


def test_archive_contains_the_payload_and_a_separate_checksum(tmp_path) -> None:
    from scripts import build_test_bundle as builder

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "manifest.json").write_text('{"schema_version": 1}\n')
    builder.write_checksums(payload)
    archive = builder.archive_payload(payload, tmp_path / "outputs", "test-build")
    assert archive.name == "test-build.tar.gz"
    assert archive.with_suffix(archive.suffix + ".sha256").read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  test-build.tar.gz\n"
    )
    with tarfile.open(archive) as bundle:
        assert {member.name for member in bundle if member.isfile()} == {
            "test-build/manifest.json",
            "test-build/SHA256SUMS",
        }


def test_image_metadata_refuses_the_wrong_architecture() -> None:
    from scripts import build_test_bundle as builder

    with pytest.raises(ValueError, match="architecture"):
        builder.image_record(
            "example:test", {"Id": "sha256:" + "a" * 64, "Architecture": "arm64", "Os": "linux"}
        )


def test_runtime_snapshot_omits_developer_only_compatibility_links(tmp_path, monkeypatch):
    from scripts import build_test_bundle as builder

    repo = tmp_path / "repo"
    _repository(repo)
    (repo / ".claude").mkdir()
    (repo / "docs/agent-rules/areas").mkdir(parents=True)
    (repo / "docs/agent-rules/areas/payments.md").write_text("# payment rules\n")
    (repo / ".claude/rules").symlink_to("../docs/agent-rules/areas")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "rules",
        ],
        cwd=repo,
        check=True,
    )
    monkeypatch.setattr(builder, "ROOT", repo)
    destination = tmp_path / "source"
    builder.source_snapshot("HEAD", destination)
    assert not (destination / ".claude").exists()
    assert (destination / "docs/agent-rules/areas/payments.md").is_file()
