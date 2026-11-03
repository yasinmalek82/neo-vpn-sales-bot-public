"""Build a versioned amd64 test bundle without reading local credentials or starting the bot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed: {arguments[0]}\n{result.stderr[-6000:]}")
    return result.stdout.strip()


def forbidden_path(path: Path) -> bool:
    return (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in {"data", "backups", "logs", ".git", ".venv", "outputs"}
        or (
            path.name.startswith(".env")
            and path.name not in {".env.example", ".env.smoke.template"}
        )
        or path.suffix.lower() in {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".dump", ".backup"}
    )


def source_snapshot(revision: str, destination: Path) -> str:
    commit = command(["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid source revision")
    result = subprocess.run(
        ["git", "archive", "--format=tar", commit], cwd=ROOT, capture_output=True, check=True
    )
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        # Developer compatibility links are not runtime inputs. All other
        # symlinks remain forbidden instead of being followed on extraction.
        members = [m for m in archive.getmembers() if Path(m.name).parts[0] != ".claude"]
        for member in members:
            path = Path(member.name)
            if forbidden_path(path) or not (member.isfile() or member.isdir()):
                raise ValueError(f"Archive contains a forbidden path: {member.name}")
        destination.mkdir(parents=True, exist_ok=False)
        # Members were checked above; avoid extractall's symlink and traversal behavior.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Missing archived source file")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o755)
    return commit


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_checksums(payload: Path) -> None:
    lines = []
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in a test bundle")
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(f"{digest(path)}  {path.relative_to(payload).as_posix()}\n")
    (payload / "SHA256SUMS").write_text("".join(lines))


def archive_payload(payload: Path, output: Path, name: str) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{name}.tar.gz"
    if destination.exists():
        raise FileExistsError(destination)
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(payload, arcname=name)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest(destination)}  {destination.name}\n"
    )
    return destination


def image_record(reference: str, metadata: dict[str, Any]) -> dict[str, str]:
    if metadata.get("Architecture") != "amd64" or metadata.get("Os") != "linux":
        raise ValueError("Bundle image architecture must be linux/amd64")
    image_id = str(metadata.get("Id", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Image has no immutable content ID")
    return {"ref": reference, "id": image_id, "architecture": "amd64"}


def inspect_image(reference: str) -> dict[str, Any]:
    return json.loads(command(["docker", "image", "inspect", reference]))[0]


def dependency_image(upstream: str, alias: str) -> dict[str, str]:
    index = json.loads(command(["docker", "buildx", "imagetools", "inspect", "--raw", upstream]))
    matches = [
        item["digest"]
        for item in index.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
        and item.get("platform", {}).get("architecture") == "amd64"
    ]
    if len(matches) != 1:
        raise ValueError(f"Cannot resolve one amd64 manifest for {upstream}")
    repository = upstream.rsplit(":", 1)[0]
    pinned = f"{repository}@{matches[0]}"
    command(["docker", "pull", "--platform", "linux/amd64", pinned])
    command(["docker", "tag", pinned, alias])
    record = image_record(alias, inspect_image(alias))
    record["upstream_digest"] = pinned
    return record


def save_images(references: list[str], destination: Path) -> None:
    with (
        destination.open("wb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped,
    ):
        process = subprocess.Popen(["docker", "image", "save", *references], stdout=subprocess.PIPE)
        if process.stdout is None:
            raise RuntimeError("No image archive stream")
        with process.stdout:
            shutil.copyfileobj(process.stdout, zipped)
        if process.wait():
            raise RuntimeError("Docker image export failed")


def record_exported_manifest_ids(archive_path: Path, images: dict[str, dict[str, str]]) -> None:
    # Containerd reports the exported OCI descriptor. Classic Docker reports
    # the configuration ID. Both identities must be pinned by the bundle.
    with tarfile.open(archive_path, "r:gz") as archive:
        index_file = archive.extractfile("index.json")
        if index_file is None:
            raise ValueError("Docker export is missing its OCI index")
        index = json.load(index_file)
    for record in images.values():
        reference = record["ref"]
        matches = [
            entry["digest"]
            for entry in index["manifests"]
            if entry.get("annotations", {}).get("io.containerd.image.name")
            in {reference, f"docker.io/library/{reference}"}
        ]
        if len(matches) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", matches[0]):
            raise ValueError(f"Cannot verify exported OCI identity for {reference}")
        record["oci_manifest_id"] = matches[0]


def build(revision: str, output: Path) -> Path:
    if command(["git", "status", "--porcelain", "--untracked-files=no"]):
        raise ValueError("Commit tracked changes before packaging")
    with tempfile.TemporaryDirectory(prefix="neo-test-bundle-") as temporary:
        payload = Path(temporary) / "payload"
        payload.mkdir()
        commit = source_snapshot(revision, payload / "source")
        short = commit[:12]
        tag = f"test/installer-{short}"
        existing = command(["git", "tag", "--list", tag])
        if existing:
            if command(["git", "rev-list", "-n", "1", tag]) != commit:
                raise ValueError("Existing test tag points at a different revision")
        else:
            command(["git", "tag", tag, commit])
        bot_reference = f"neo-bot-test:{short}"
        print("Building the committed application for linux/amd64...", flush=True)
        command(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--load",
                "--tag",
                bot_reference,
                "--label",
                f"org.opencontainers.image.revision={commit}",
                "--file",
                str(payload / "source/docker/Dockerfile"),
                str(payload / "source"),
            ]
        )
        images = {"bot": image_record(bot_reference, inspect_image(bot_reference))}
        for name, upstream in (("db", "postgres:16-alpine"), ("caddy", "caddy:2-alpine")):
            print(f"Resolving and loading the amd64 {name} image...", flush=True)
            images[name] = dependency_image(upstream, f"neo-test-{name}:{short}")
        manifest = {
            "schema_version": 1,
            "source_revision": commit,
            "source_tag": tag,
            "platform": "linux/amd64",
            "images": images,
        }
        for source, destination in (
            ("install.sh", "install.sh"),
            ("docker/docker-compose.test.yml", "compose.yml"),
            ("docs/TEST_INSTALL_FA.md", "README.fa.md"),
        ):
            shutil.copy2(payload / "source" / source, payload / destination)
        guide = payload / "README.fa.md"
        guide.write_text(
            guide.read_text()
            .replace("BUNDLE_NAME", f"neo-test-install-linux-amd64-{short}")
            .replace("RELEASE_TAG", tag)
        )
        print("Exporting the images and checksums...", flush=True)
        save_images([record["ref"] for record in images.values()], payload / "images.tar.gz")
        record_exported_manifest_ids(payload / "images.tar.gz", images)
        (payload / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        write_checksums(payload)
        return archive_payload(payload, output, f"neo-test-install-linux-amd64-{short}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(build(arguments.revision, arguments.output_dir.resolve()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
