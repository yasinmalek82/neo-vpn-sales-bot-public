"""Download one pinned public GitHub release, verify it, then run its installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.github.com/repos/yasinmalek82/neo-vpn-sales-bot-public"
MAX_ARCHIVE_BYTES = 2 * 1024**3


class DownloadError(Exception):
    """A failure safe to display without private URLs or response bodies."""


class PrivateRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        parsed = urlsplit(newurl)
        allowed = {
            "api.github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
            "raw.githubusercontent.com",
        }
        if parsed.scheme != "https" or parsed.hostname not in allowed:
            raise DownloadError("Unsafe download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def verify_download(archive: Path, checksum: str) -> None:
    match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(archive.name) + r"\n?", checksum)
    if match is None:
        raise DownloadError("Invalid checksum metadata")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != match[1]:
        raise DownloadError("Download checksum mismatch; nothing was installed")


def download_release(
    tag: str, token: str, destination: Path, *, open_request: Callable[..., Any] | None = None
) -> Path:
    if not re.fullmatch(r"test/installer-[0-9a-f]{12}", tag):
        raise DownloadError("Invalid pinned release tag")
    opener = open_request or build_opener(PrivateRedirect()).open

    def request(url: str, accept: str) -> Request:
        headers = {
            "Accept": accept,
            "User-Agent": "NEO-test-installer",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return Request(url, headers=headers)

    with opener(
        request(f"{API}/releases/tags/{quote(tag, safe='')}", "application/vnd.github+json"),
        timeout=60,
    ) as response:
        metadata = json.loads(response.read(1024 * 1024 + 1))
    if not isinstance(metadata, dict) or metadata.get("tag_name") != tag or metadata.get("draft"):
        raise DownloadError("The pinned release is not published")
    name = f"neo-test-install-linux-amd64-{tag.rsplit('-', 1)[1]}.tar.gz"
    entries = metadata.get("assets")
    if not isinstance(entries, list):
        raise DownloadError("Release asset metadata is invalid")
    assets = {
        item["name"]: item["id"]
        for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and type(item.get("id")) is int
        and item["id"] > 0
    }
    if name not in assets or name + ".sha256" not in assets:
        raise DownloadError("Release bundle or checksum is missing")
    with opener(
        request(
            f"{API}/releases/assets/{int(assets[name + '.sha256'])}", "application/octet-stream"
        ),
        timeout=60,
    ) as response:
        checksum = response.read(4097).decode("ascii")
    if len(checksum) > 4096:
        raise DownloadError("Invalid checksum metadata")
    archive = destination / name
    if archive.is_symlink() or (archive.exists() and not archive.is_file()):
        raise DownloadError("Cached archive is not a regular file")
    if archive.is_file():
        try:
            verify_download(archive, checksum)
        except DownloadError:
            print("Cached bundle failed verification; downloading a fresh copy...", flush=True)
        else:
            print("Using the verified cached bundle.", flush=True)
            return archive
    total = 0
    with tempfile.TemporaryDirectory(prefix=".neo-download-", dir=destination) as temporary:
        partial = Path(temporary) / name
        with (
            opener(
                request(f"{API}/releases/assets/{int(assets[name])}", "application/octet-stream"),
                timeout=120,
            ) as response,
            partial.open("xb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise DownloadError("Release archive is too large")
                output.write(chunk)
                if total % (16 * 1024 * 1024) < len(chunk):
                    print(f"Downloaded {total // (1024 * 1024)} MiB...", flush=True)
        verify_download(partial, checksum)
        partial.replace(archive)
    return archive


def extract_bundle(archive: Path, destination: Path, name: str) -> Path:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        seen = set()
        if len(members) > 20000 or sum(member.size for member in members) > 4 * 1024**3:
            raise DownloadError("Bundle is too large to extract")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != name
                or not (member.isfile() or member.isdir())
                or path.as_posix() in seen
            ):
                raise DownloadError("Bundle contains an unsafe archive entry")
            seen.add(path.as_posix())
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                content = source.extractfile(member)
                if content is None:
                    raise DownloadError("Missing archive file content")
                with content, target.open("xb") as output:
                    shutil.copyfileobj(content, output)
                target.chmod(member.mode & 0o755)
    return destination / name


def prepare_cache(cache: Path) -> None:
    if cache.is_symlink():
        raise DownloadError("Download cache cannot be a symlink")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = cache.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise DownloadError("Download cache must be a private directory owned by the current user")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/neo-test"))
    parser.add_argument("--cache-dir", type=Path, default=Path("/var/cache/neo-test/downloads"))
    parser.add_argument(
        "action", nargs="?", choices=("install", "status", "start", "stop", "update", "uninstall")
    )
    swap = parser.add_mutually_exclusive_group()
    swap.add_argument("--swap-1g", action="store_true")
    swap.add_argument("--no-swap", action="store_true")
    args = parser.parse_args(argv)
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or os.uname().machine not in {"x86_64", "amd64"}
    ):
        print("Run with sudo on an Ubuntu amd64 test server.", file=sys.stderr)
        return 1
    token = globals().pop("_NEO_GITHUB_TOKEN", "") or ""
    try:
        cache = args.cache_dir
        prepare_cache(cache)
        with tempfile.TemporaryDirectory(prefix="neo-release-download-") as temporary:
            work = Path(temporary)
            print("Downloading the pinned release and verifying its SHA256 checksum...", flush=True)
            archive = download_release(args.release, token.strip(), cache)
            bundle = extract_bundle(
                archive, work / "unpacked", archive.name.removesuffix(".tar.gz")
            )
            flags = ["--swap-1g"] if args.swap_1g else ["--no-swap"] if args.no_swap else []
            return subprocess.run(
                [
                    "/bin/sh",
                    str(bundle / "install.sh"),
                    "--install-dir",
                    str(args.install_dir),
                    *flags,
                    *([args.action] if args.action else []),
                ],
                check=False,
            ).returncode
    except (KeyboardInterrupt, EOFError):
        print(
            "Installer: Cancelled. Verified downloads are kept for the next attempt.",
            file=sys.stderr,
        )
        return 130
    except DownloadError as exc:
        print(f"Installer: {exc}", file=sys.stderr)
    except (OSError, ValueError, tarfile.TarError):
        print(
            "Release download failed. Check GitHub availability, connectivity and free disk space; no credentials were displayed.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
