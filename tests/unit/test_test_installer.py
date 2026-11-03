"""Behavioral checks for the offline test installer runtime."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import install_probe
from scripts.test_installer import (
    Installer,
    InstallerError,
    _official_docker_install,
    _parse_probe,
    _validate_domain,
    _validate_secret,
    load_bundle,
    quote_compose_env,
    validate_admin_ids,
    validate_https_url,
)


def _bundle(root: Path) -> Path:
    bundle = root / "bundle"
    (bundle / "source" / "scripts").mkdir(parents=True)
    (bundle / "compose.yml").write_text(
        "services:\n  bot:\n    image: ${NEO_BOT_IMAGE}\n  db:\n    image: ${NEO_DB_IMAGE}\n"
        "  caddy:\n    image: ${NEO_CADDY_IMAGE}\n",
        encoding="utf-8",
    )
    (bundle / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (bundle / "source" / "scripts" / "marker.py").write_text("# source\n", encoding="utf-8")
    (bundle / "images.tar.gz").write_bytes(b"test archive")
    images = {
        name: {
            "ref": f"neo-test-{name}:latest",
            "id": f"sha256:{index:064x}",
            "architecture": "amd64",
        }
        for index, name in enumerate(("bot", "db", "caddy"), 1)
    }
    manifest = {
        "schema_version": 1,
        "source_revision": "a" * 40,
        "source_tag": "test/installer-aaaaaaaaaaaa",
        "platform": "linux/amd64",
        "images": images,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    files = [
        "compose.yml",
        "images.tar.gz",
        "install.sh",
        "manifest.json",
        "source/scripts/marker.py",
    ]
    checksums = "".join(
        f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n" for name in files
    )
    (bundle / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return bundle


def test_load_bundle_rejects_a_payload_changed_after_checksum_generation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "compose.yml").write_text("services: {bot: {}}\n", encoding="utf-8")

    with pytest.raises(InstallerError, match="checksum"):
        load_bundle(bundle)


def test_load_bundle_hashes_image_archive_without_reading_it_all_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    original = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.name == "images.tar.gz":
            raise AssertionError("large image archive must be streamed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    assert load_bundle(bundle).manifest["platform"] == "linux/amd64"


def test_load_bundle_rejects_symlink_payload_even_when_it_points_inside_bundle(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    target = bundle / "source" / "scripts" / "marker.py"
    target.unlink()
    real_target = bundle / "source" / "marker.py"
    real_target.write_text("# source\n", encoding="utf-8")
    target.symlink_to("../marker.py")
    lines = []
    for relative in (
        "compose.yml",
        "images.tar.gz",
        "install.sh",
        "manifest.json",
        "source/scripts/marker.py",
    ):
        lines.append(
            f"{hashlib.sha256(target.read_bytes() if relative.endswith('marker.py') else (bundle / relative).read_bytes()).hexdigest()}  {relative}\n"
        )
    (bundle / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    with pytest.raises(InstallerError, match="symlink"):
        load_bundle(bundle)


@pytest.mark.parametrize("value", ["", "  ", "2,2", "0", "-4", "1,9223372036854775808", "abc"])
def test_admin_ids_reject_invalid_or_duplicate_identifiers(value: str) -> None:
    with pytest.raises(ValueError):
        validate_admin_ids(value)


def test_admin_ids_accepts_positive_signed_64_bit_values() -> None:
    assert validate_admin_ids("41, 9223372036854775807") == "41,9223372036854775807"


@pytest.mark.parametrize("value", ["", "  ", "token\nother", "token\rother"])
def test_secret_inputs_reject_empty_values_and_line_breaks(value: str) -> None:
    with pytest.raises(ValueError):
        _validate_secret(value, "Secret")


@pytest.mark.parametrize(
    "value",
    [
        "http://panel.example",
        "https://u:p@panel.example",
        "https://panel.example/x?q=1",
        "https://panel.example/#frag",
    ],
)
def test_panel_url_rejects_non_public_or_ambiguous_urls(value: str) -> None:
    with pytest.raises(ValueError):
        validate_https_url(value)


def test_test_panel_requires_a_dns_name_instead_of_an_ip_literal() -> None:
    with pytest.raises(ValueError, match="DNS"):
        _validate_domain("203.0.113.9")
    assert _validate_domain("Test.Example.COM.") == "test.example.com"


def test_panel_url_accepts_https_origin_with_optional_port() -> None:
    assert validate_https_url("https://panel.example:8443/") == "https://panel.example:8443"


@pytest.mark.parametrize(
    "value",
    ["quote' slash\\ dollar$", "trailing\\", '"$HOME', "slash\\'quote", "plain"],
)
def test_compose_env_serialization_round_trips_escaped_values(value: str) -> None:
    encoded = quote_compose_env(value)

    assert json.loads(encoded.replace("$$", "$")) == value
    assert "$$" in encoded if "$" in value else "$" not in encoded


def test_persian_card_holder_is_preserved_by_real_env_parser() -> None:
    import io

    from dotenv import dotenv_values

    holder = "صاحب کارت آزمایشی"
    parsed = dotenv_values(
        stream=io.StringIO("PAYMENT_CARD_HOLDER_NAME=" + quote_compose_env(holder))
    )
    assert parsed["PAYMENT_CARD_HOLDER_NAME"] == holder


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"args": args, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_existing_matching_install_reports_status_without_rewriting_credentials_or_database(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    target = tmp_path / "install"
    target.mkdir()
    (target / "manifest.json").write_bytes((bundle / "manifest.json").read_bytes())
    (target / "compose.yml").write_bytes((bundle / "compose.yml").read_bytes())
    (target / "install.sh").write_bytes((bundle / "install.sh").read_bytes())
    (target / "source").mkdir()
    secret_env = "BOT_TOKEN='secret-original'\n"
    (target / ".env").write_text(secret_env, encoding="utf-8")
    db = target / "db_data"
    db.mkdir()
    marker = db / "owner.txt"
    marker.write_text("preserve", encoding="utf-8")
    runner = FakeRunner()
    output: list[str] = []
    installer = Installer(bundle, target, runner=runner, output=output.append)
    installer.host_check = lambda: None

    installer.run("install")

    assert (target / ".env").read_text(encoding="utf-8") == secret_env
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert len(runner.calls) == 1
    assert runner.calls[0]["args"][-1] == "ps"
    assert any("already exists" in line.lower() for line in output)


def test_secret_probe_failure_redacts_probe_output_and_does_not_write_install_files(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    token = "secret-token-never-print"
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                [], 0, stdout='{"ok":false,"error":"telegram unavailable"}', stderr=""
            ),
        ]
    )
    answers = iter([token])
    output: list[str] = []
    installer = Installer(
        bundle,
        tmp_path / "install",
        runner=runner,
        output=output.append,
        prompt=lambda _label: next(answers),
        secret_prompt=lambda _label: next(answers),
        host_check=lambda: None,
        preflight=lambda _bundle: None,
        ensure_docker=lambda: None,
    )

    with pytest.raises(InstallerError):
        installer.run("install")

    assert not (tmp_path / "install").exists()
    assert token not in "\n".join(output)
    assert token not in repr(runner.calls[0]["args"])


def test_invalid_admin_entry_reprompts_before_accepting_the_next_value(tmp_path: Path) -> None:
    prompts = iter(["0", "44"])
    output: list[str] = []
    installer = Installer(
        tmp_path, tmp_path / "install", prompt=lambda _label: next(prompts), output=output.append
    )

    assert installer._ask("Admin: ", validate_admin_ids) == "44"
    assert len(output) == 1


def test_webhook_bot_is_rejected_before_install_directory_or_compose_start(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    token = "secret-token"
    runner = FakeRunner(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            *[
                subprocess.CompletedProcess([], 0, stdout=f"{image['id']} amd64\n", stderr="")
                for image in load_bundle(bundle).images.values()
            ],
            subprocess.CompletedProcess(
                [], 0, stdout='{"ok":true,"username":"test_bot","has_webhook":true}', stderr=""
            ),
        ]
    )
    installer = Installer(
        bundle,
        tmp_path / "install",
        runner=runner,
        secret_prompt=lambda _label: token,
        host_check=lambda: None,
        preflight=lambda _bundle: None,
        ensure_docker=lambda: None,
    )

    with pytest.raises(InstallerError, match="webhook"):
        installer.run("install")

    assert not (tmp_path / "install").exists()
    assert len(runner.calls) == 5
    assert runner.calls[-1]["args"][-2:] == ["scripts.install_probe", "telegram"]
    assert token not in repr(runner.calls[-1]["args"])


def test_wrong_image_identity_aborts_before_asking_for_credentials_or_running_probes(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    runner = FakeRunner(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"sha256:{'f' * 64} arm64\n", stderr=""),
        ]
    )
    secrets: list[str] = []
    installer = Installer(
        bundle,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: pytest.fail("credentials were requested before image verification"),
        secret_prompt=lambda _label: secrets.append("prompted") or "never",
        host_check=lambda: None,
        preflight=lambda _bundle: None,
        ensure_docker=lambda: None,
    )

    with pytest.raises(InstallerError, match="Loaded bot image"):
        installer.run("install")

    assert secrets == []
    assert len(runner.calls) == 2
    assert all(call["args"][1] != "run" for call in runner.calls)
    assert not (tmp_path / "install").exists()


def test_dns_mismatch_aborts_valid_install_before_config_or_start(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    install_dir = tmp_path / "install"
    prompts = iter(
        ["test_bot", "42", "42", "https://panel.example", "example.com", "8.8.8.8", "n", "y"]
    )
    secrets = iter(["telegram-secret", "pg_key_private"])
    calls: list[list[str]] = []
    image_by_ref = {item["ref"]: item["id"] for item in load_bundle(bundle).images.values()}

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1] == "run":
            payload = (
                {"ok": True, "username": "test_bot", "has_webhook": False}
                if args[-1] == "telegram"
                else {"ok": True}
            )
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{image_by_ref[args[3]]} amd64\n", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def resolver(_domain: str, _port: int, **_kwargs: Any) -> list[Any]:
        return [(None, None, None, None, ("203.0.113.20", 443))]

    installer = Installer(
        bundle,
        install_dir,
        runner=runner,
        prompt=lambda _label: next(prompts),
        secret_prompt=lambda _label: next(secrets),
        host_check=lambda: None,
        preflight=lambda _bundle: None,
        ensure_docker=lambda: None,
        resolve=resolver,
    )

    with pytest.raises(InstallerError, match="DNS"):
        installer.run("install")

    assert not install_dir.exists()
    assert not (install_dir / ".env").exists()
    assert not any(args[1:3] == ["compose", "--project-name"] for args in calls)


def test_env_file_is_private_and_escapes_compose_special_characters(tmp_path: Path) -> None:
    target = tmp_path / "install"
    target.mkdir()
    installer = Installer(tmp_path, target)

    installer._write_env({"BOT_TOKEN": "a'b\\$c"})

    assert (target / ".env").stat().st_mode & 0o777 == 0o600
    assert (target / ".env").read_text(encoding="utf-8") == 'BOT_TOKEN="a\'b\\\\$$c"\n'


def test_dns_mismatch_is_rejected_before_any_install_write(tmp_path: Path) -> None:
    installer = Installer(
        tmp_path,
        tmp_path / "install",
        resolve=lambda *_args, **_kwargs: [(None, None, None, None, ("203.0.113.9", 443))],
    )

    with pytest.raises(InstallerError, match="DNS"):
        installer._verify_dns("panel.example", "203.0.113.8")

    assert not (tmp_path / "install").exists()


def test_probe_failure_discards_raw_secret_bearing_response() -> None:
    secret = "pg_key_sensitive-value"
    result = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"ok": False, "detail": secret}), stderr=""
    )

    with pytest.raises(InstallerError) as caught:
        _parse_probe(result)

    assert secret not in str(caught.value)


def test_docker_bootstrap_installs_curl_and_gpg_before_requiring_them(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    installed: set[str] = set()
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=ubuntu\nVERSION_CODENAME=jammy\n", encoding="utf-8")
    source_file = tmp_path / "apt" / "docker.list"
    source_file.parent.mkdir(parents=True)

    def which(name: str) -> str | None:
        if name == "docker":
            return None
        return f"/usr/bin/{name}" if name in installed else None

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["apt-get", "install"]:
            installed.update({"curl", "gpg"})
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    _official_docker_install(
        runner,
        which=which,
        os_release_file=os_release,
        source_file=source_file,
    )

    assert calls[0] == ["apt-get", "update"]
    assert calls[1][:3] == ["apt-get", "install", "-y"]
    assert (
        calls.index(
            [
                "apt-get",
                "install",
                "-y",
                "docker-ce",
                "docker-ce-cli",
                "containerd.io",
                "docker-buildx-plugin",
                "docker-compose-plugin",
            ]
        )
        > 1
    )
    assert source_file.read_text(encoding="utf-8") == (
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] "
        "https://download.docker.com/linux/ubuntu jammy stable\n"
    )
    assert all("curl |" not in " ".join(args) for args in calls)


@pytest.mark.asyncio
async def test_telegram_probe_reports_webhook_and_closes_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        closed = False

        async def close(self) -> None:
            self.closed = True

    session = Session()

    class Bot:
        def __init__(self, token: str) -> None:
            assert token == "token-in-stdin"
            self.session = session

        async def get_me(self) -> Any:
            return SimpleNamespace(username="test_bot")

        async def get_webhook_info(self) -> Any:
            return SimpleNamespace(url="https://existing.example/webhook")

    monkeypatch.setattr("aiogram.Bot", Bot)

    result = await install_probe._telegram("token-in-stdin")

    assert result == {"ok": True, "username": "test_bot", "has_webhook": True}
    assert session.closed


@pytest.mark.asyncio
async def test_panel_probe_closes_client_when_read_only_check_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"closed": False}

    class Client:
        def __init__(self, *, base_url: str, api_key: str) -> None:
            assert base_url == "https://panel.example"
            assert api_key == "pg_key_test"

        async def get_current_admin(self) -> None:
            raise RuntimeError("sensitive api_key response")

        async def aclose(self) -> None:
            state["closed"] = True

    monkeypatch.setattr("api_client.client.PasarGuardClient", Client)

    with pytest.raises(RuntimeError, match="sensitive"):
        await install_probe._panel("https://panel.example", "pg_key_test")

    assert state["closed"]


def test_probe_cli_suppresses_library_exception_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "pg_key_do_not_print"
    disabled: list[int] = []
    monkeypatch.setattr("scripts.install_probe.logging.disable", disabled.append)

    async def fail(_kind: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(secret)

    monkeypatch.setattr(install_probe, "_dispatch", fail)
    monkeypatch.setattr("scripts.install_probe.sys.stdin", SimpleNamespace(read=lambda: "{}"))

    assert install_probe.main(["panel"]) == 1

    assert capsys.readouterr().out == '{"ok":false,"error":"credential_check_failed"}\n'
    assert secret not in capsys.readouterr().err
    assert disabled == [install_probe.logging.CRITICAL]


@pytest.mark.parametrize("sms_enabled", [False, True])
def test_successful_install_loads_pinned_images_validates_compose_and_writes_expected_config(
    tmp_path: Path,
    sms_enabled: bool,
) -> None:
    bundle = _bundle(tmp_path)
    install_dir = tmp_path / "install"
    token = "telegram-secret"
    panel_key = "pg_key_private"
    answers = ["test_bot", "42", "42", "https://panel.example", "example.com", "8.8.8.8"]
    answers += ["y", "6219861234567890", "Test Holder"] if sms_enabled else ["n"]
    prompts = iter([*answers, "y"])
    secrets = iter([token, panel_key])
    calls: list[dict[str, Any]] = []
    image_by_ref = {item["ref"]: item["id"] for item in load_bundle(bundle).images.values()}

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        if args[1] == "run":
            kind = args[-1]
            data = (
                {"ok": True, "username": "test_bot", "has_webhook": False}
                if kind == "telegram"
                else {"ok": True}
            )
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data), stderr="")
        if args[1:3] == ["image", "inspect"]:
            ref = args[3]
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{image_by_ref[ref]} amd64\n", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def resolver(_domain: str, _port: int, **_kwargs: Any) -> list[Any]:
        return [(None, None, None, None, ("8.8.8.8", 443))]

    installer = Installer(
        bundle,
        install_dir,
        runner=runner,
        prompt=lambda _label: next(prompts),
        secret_prompt=lambda _label: next(secrets),
        host_check=lambda: None,
        preflight=lambda _bundle: None,
        ensure_docker=lambda: None,
        resolve=resolver,
    )

    installer.run("install")

    args = [call["args"] for call in calls]
    assert args[0][:3] == ["docker", "load", "-i"]
    assert [item[0] for item in args if item[1:3] == ["image", "inspect"]] == [
        "docker",
        "docker",
        "docker",
    ]
    assert all(
        args.index(["docker", "load", "-i", str(bundle / "images.tar.gz")]) < i
        for i, item in enumerate(args)
        if item[1:2] == ["run"]
    )
    config_index = next(
        i
        for i, item in enumerate(args)
        if item[1:3] == ["compose", "--project-name"] and item[-2:] == ["config", "--quiet"]
    )
    up_index = next(
        i
        for i, item in enumerate(args)
        if item[1:3] == ["compose", "--project-name"] and "up" in item
    )
    assert config_index < up_index
    assert all("build" not in item and item[:2] != ["docker", "pull"] for item in args)
    assert all(token not in arg and panel_key not in arg for item in args for arg in item)
    env_path = install_dir / ".env"
    assert env_path.stat().st_mode & 0o777 == 0o600
    env = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
    env = {key: json.loads(value.replace("$$", "$")) for key, value in env.items()}
    password = env["POSTGRES_PASSWORD"]
    assert env["POSTGRES_DB"] == "neo_test"
    assert env["POSTGRES_USER"] == "neo_test"
    assert env["DB_PASSWORD"] == password
    assert env["BANK_SMS_ENABLED"] == str(sms_enabled).lower()
    if sms_enabled:
        assert env["BANK_SMS_SENDER"] == "BLUE"
        assert env["BANK_SMS_URL"] == "https://example.com/bank-sms/blu"
        assert len(env["BANK_SMS_TOKEN"]) >= 32
        assert env["BANK_SMS_ACCOUNT_KEY"] == "blu-main"
        assert env["PAYMENT_CARD_NUMBER"] == "6219861234567890"
        assert env["PAYMENT_CARD_HOLDER_NAME"] == "Test Holder"
        private = install_dir / "shortcut-settings.json"
        assert private.stat().st_mode & 0o777 == 0o600
        assert json.loads(private.read_text())["token"] == env["BANK_SMS_TOKEN"]
    assert env["DATABASE_URL"] == f"postgresql+asyncpg://neo_test:{password}@db:5432/neo_test"
    assert env["WEB_PANEL_URL"] == "https://example.com/panel/"
    assert env["BOT_TOKEN"] == token
    assert env["PASARGUARD_API_KEY"] == panel_key
