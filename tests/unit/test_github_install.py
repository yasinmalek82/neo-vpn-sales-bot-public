"""The one-command downloader must verify bytes before executing anything."""

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest


def test_checksum_mismatch_is_rejected_before_unpack(tmp_path):
    from scripts.github_install import DownloadError, verify_download

    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"wrong archive")
    with pytest.raises(DownloadError, match="checksum"):
        verify_download(archive, "a" * 64 + "  bundle.tar.gz\n")


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("bundle/secret", "symlink"),
        ("bundle/device", "device"),
    ],
)
def test_untrusted_archive_paths_and_links_are_rejected(tmp_path, name, kind):
    from scripts.github_install import DownloadError, extract_bundle

    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        else:
            member.size = 4
        output.addfile(member, io.BytesIO(b"test") if kind == "file" else None)
    with pytest.raises(DownloadError):
        extract_bundle(archive, tmp_path / "out", "bundle")
    assert not (tmp_path / "escape").exists()


def test_verified_bundle_extracts_only_inside_expected_directory(tmp_path):
    from scripts.github_install import extract_bundle, verify_download

    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("bundle/install.sh")
        member.size, member.mode = 4, 0o755
        output.addfile(member, io.BytesIO(b"test"))
    verify_download(archive, hashlib.sha256(archive.read_bytes()).hexdigest() + "  bundle.tar.gz\n")
    directory = extract_bundle(archive, tmp_path / "out", "bundle")
    assert (directory / "install.sh").read_bytes() == b"test"


def test_redirect_strips_github_credentials_and_refuses_http():
    from urllib.request import Request

    from scripts.github_install import DownloadError, PrivateRedirect

    redirect = PrivateRedirect()
    request = Request("https://api.github.com/a", headers={"Authorization": "Bearer private"})
    result = redirect.redirect_request(
        request, None, 302, "", {}, "https://release-assets.githubusercontent.com/file"
    )
    assert result.get_header("Authorization") is None
    with pytest.raises(DownloadError):
        redirect.redirect_request(request, None, 302, "", {}, "http://example.com/file")


def test_download_release_verifies_asset_before_returning(tmp_path):
    from scripts.github_install import download_release

    name = "neo-test-install-linux-amd64-aaaaaaaaaaaa.tar.gz"
    data = b"archive bytes"
    checksum = hashlib.sha256(data).hexdigest() + "  " + name + "\n"
    responses = [
        json.dumps(
            {
                "tag_name": "test/installer-aaaaaaaaaaaa",
                "draft": False,
                "assets": [{"name": name, "id": 11}, {"name": name + ".sha256", "id": 12}],
            }
        ).encode(),
        checksum.encode(),
        data,
    ]
    requests = []

    def open_request(request, **kwargs):
        requests.append(request)
        return io.BytesIO(responses.pop(0))

    archive = download_release(
        "test/installer-aaaaaaaaaaaa", "private-token", tmp_path, open_request=open_request
    )
    assert archive.read_bytes() == data
    assert [request.full_url for request in requests] == [
        "https://api.github.com/repos/yasinmalek82/neo-vpn-sales-bot-public/releases/tags/test%2Finstaller-aaaaaaaaaaaa",
        "https://api.github.com/repos/yasinmalek82/neo-vpn-sales-bot-public/releases/assets/12",
        "https://api.github.com/repos/yasinmalek82/neo-vpn-sales-bot-public/releases/assets/11",
    ]


def test_public_release_download_needs_no_authorization(tmp_path):
    """Sending an empty Bearer header breaks anonymous GitHub downloads."""
    from scripts.github_install import download_release

    name = "neo-test-install-linux-amd64-bbbbbbbbbbbb.tar.gz"
    data = b"public archive"
    responses = [
        json.dumps(
            {
                "tag_name": "test/installer-bbbbbbbbbbbb",
                "draft": False,
                "assets": [{"name": name, "id": 21}, {"name": name + ".sha256", "id": 22}],
            }
        ).encode(),
        (hashlib.sha256(data).hexdigest() + "  " + name + "\n").encode(),
        data,
    ]

    def open_request(request, **kwargs):
        assert request.get_header("Authorization") is None
        assert request.full_url.startswith(
            "https://api.github.com/repos/yasinmalek82/neo-vpn-sales-bot-public/"
        )
        return io.BytesIO(responses.pop(0))

    archive = download_release(
        "test/installer-bbbbbbbbbbbb", "", tmp_path, open_request=open_request
    )
    assert archive.read_bytes() == data


def test_public_entrypoint_does_not_prompt_for_github_token(monkeypatch, tmp_path):
    from scripts import github_install

    monkeypatch.setattr(github_install.sys, "platform", "linux")
    monkeypatch.setattr(github_install.os, "geteuid", lambda: 0)
    uname = SimpleNamespace(machine="x86_64")
    monkeypatch.setattr(github_install.os, "uname", lambda: uname)

    def unexpected_prompt(*args):
        pytest.fail("Public installer must not ask for a GitHub token")

    monkeypatch.setattr("getpass.getpass", unexpected_prompt)

    reached_download = []
    monkeypatch.setattr(github_install, "prepare_cache", lambda _path: None)

    def offline_download(tag, token, destination):
        reached_download.append(tag)
        assert token == ""
        raise github_install.DownloadError("offline fixture")

    monkeypatch.setattr(github_install, "download_release", offline_download)
    assert (
        github_install.main(
            ["--release", "test/installer-bbbbbbbbbbbb", "--cache-dir", str(tmp_path / "cache")]
        )
        == 1
    )
    assert reached_download == ["test/installer-bbbbbbbbbbbb"]


def test_verified_cached_release_does_not_download_archive_again(tmp_path):
    from scripts.github_install import download_release

    name = "neo-test-install-linux-amd64-cccccccccccc.tar.gz"
    data = b"verified cached archive"
    archive = tmp_path / name
    archive.write_bytes(data)
    responses = [
        json.dumps(
            {
                "tag_name": "test/installer-cccccccccccc",
                "draft": False,
                "assets": [{"name": name, "id": 31}, {"name": name + ".sha256", "id": 32}],
            }
        ).encode(),
        (hashlib.sha256(data).hexdigest() + "  " + name + "\n").encode(),
    ]
    requests = []

    def opener(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(responses.pop(0))

    assert (
        download_release("test/installer-cccccccccccc", "", tmp_path, open_request=opener)
        == archive
    )
    assert archive.read_bytes() == data
    assert len(requests) == 2


def test_interrupted_download_does_not_leave_a_reusable_partial_archive(tmp_path):
    from scripts.github_install import download_release

    name = "neo-test-install-linux-amd64-dddddddddddd.tar.gz"
    responses = [
        json.dumps(
            {
                "tag_name": "test/installer-dddddddddddd",
                "draft": False,
                "assets": [{"name": name, "id": 41}, {"name": name + ".sha256", "id": 42}],
            }
        ).encode(),
        (hashlib.sha256(b"complete").hexdigest() + "  " + name + "\n").encode(),
    ]

    class BrokenDownload(io.BytesIO):
        def read(self, *args):
            super().read(*args)
            raise OSError("interrupted transfer")

    def opener(request, **kwargs):
        return io.BytesIO(responses.pop(0)) if responses else BrokenDownload(b"partial")

    with pytest.raises(OSError):
        download_release("test/installer-dddddddddddd", "", tmp_path, open_request=opener)
    assert not (tmp_path / name).exists()
    assert list(tmp_path.iterdir()) == []


def test_download_cache_rejects_shared_permissions_and_symlinks(tmp_path):
    from scripts.github_install import DownloadError, prepare_cache

    cache = tmp_path / "cache"
    prepare_cache(cache)
    assert cache.stat().st_mode & 0o777 == 0o700
    cache.chmod(0o777)
    with pytest.raises(DownloadError, match="private"):
        prepare_cache(cache)
    link = tmp_path / "link"
    link.symlink_to(cache, target_is_directory=True)
    with pytest.raises(DownloadError):
        prepare_cache(link)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {"tag_name": "test/installer-eeeeeeeeeeee", "assets": None},
        {"tag_name": "test/installer-eeeeeeeeeeee", "assets": [{"name": "bad"}]},
    ],
)
def test_malformed_release_metadata_is_reported_without_a_traceback(tmp_path, metadata):
    from scripts.github_install import DownloadError, download_release

    def opener(_request, **_kwargs):
        return io.BytesIO(json.dumps(metadata).encode())

    with pytest.raises(DownloadError):
        download_release("test/installer-eeeeeeeeeeee", "", tmp_path, open_request=opener)
