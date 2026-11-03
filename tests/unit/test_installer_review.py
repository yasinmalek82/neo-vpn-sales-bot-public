"""Regressions for interrupted setup, explicit choices and Telegram handover."""

import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import install_probe
from scripts.test_installer import Installer, InstallerError, load_bundle
from tests.unit.test_test_installer import FakeRunner, _bundle


def test_webhook_setup_requires_identity_and_defers_mutation(tmp_path):
    bundle = load_bundle(_bundle(tmp_path))
    answers = iter(
        ["test_bot", "1", "42", "", "https://panel.example", "test.example.com", "8.8.8.8", "n"]
    )
    secrets = iter(["token-private", "panel-private"])
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True, "username": "test_bot", "has_webhook": True})
            ),
            subprocess.CompletedProcess([], 0, '{"ok":true}'),
        ]
    )
    messages = []
    installer = Installer(
        bundle.root,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: next(answers),
        secret_prompt=lambda _label: next(secrets),
        output=messages.append,
    )
    values = installer._collect_settings(bundle)
    assert [call["args"][-1] for call in runner.calls] == [
        "telegram",
        "panel",
    ]
    assert json.loads(runner.calls[1]["input"]) == {
        "base_url": "https://panel.example",
        "api_key": "panel-private",
    }
    assert values["BOT_USERNAME"] == "test_bot"
    assert values["POLLING_DROP_PENDING_UPDATES"] == "false"
    assert "token-private" not in "\n".join(messages)
    assert "panel-private" not in "\n".join(messages)


@pytest.mark.asyncio
async def test_webhook_removal_keeps_pending_updates_and_rechecks(monkeypatch):
    import aiogram

    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
        get_webhook_info=AsyncMock(
            side_effect=[SimpleNamespace(url="private"), SimpleNamespace(url="")]
        ),
        delete_webhook=AsyncMock(return_value=True),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(aiogram, "Bot", lambda **_kwargs: bot)
    result = await install_probe._dispatch(
        "telegram-remove-webhook", {"token": "secret", "expected_username": "test_bot"}
    )
    assert result["ok"] is True and result["has_webhook"] is False
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    assert bot.get_webhook_info.await_count == 2
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_removal_refuses_a_different_bot(monkeypatch):
    import aiogram

    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="other_bot")),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="private")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(aiogram, "Bot", lambda **_kwargs: bot)
    result = await install_probe._dispatch(
        "telegram-remove-webhook", {"token": "secret", "expected_username": "test_bot"}
    )
    assert result["ok"] is False
    bot.delete_webhook.assert_not_awaited()
    bot.session.close.assert_awaited_once()


def test_boolean_choice_reprompts_a_typo_instead_of_enabling_payments(tmp_path):
    answers = iter(["maybe", "n"])
    installer = Installer(
        tmp_path,
        tmp_path / "install",
        prompt=lambda _label: next(answers),
        output=lambda _message: None,
    )
    assert installer._confirm("Enable?", default=True) is False


def test_failed_copy_does_not_leave_an_unusable_install_directory(tmp_path, monkeypatch):
    from scripts import test_installer

    bundle = load_bundle(_bundle(tmp_path))
    target = tmp_path / "install"
    installer = Installer(bundle.root, target)

    def interrupted_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(test_installer.shutil, "copytree", interrupted_copy)
    with pytest.raises(OSError):
        installer._install(bundle, {})
    assert not target.exists()


@pytest.mark.parametrize("extra", ["8.8.4.4", "2001:4860:4860::8888"])
def test_dns_refuses_mixed_destinations_that_can_break_tls(tmp_path, extra):
    def resolver(*_args, **_kwargs):
        return [
            (socket.AF_INET, 1, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET6 if ":" in extra else socket.AF_INET, 1, 6, "", (extra, 443)),
        ]

    installer = Installer(tmp_path, tmp_path / "install", resolve=resolver)
    with pytest.raises(InstallerError, match="DNS|AAAA"):
        installer._verify_dns("test.example.com", "8.8.8.8")


@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.asyncio
async def test_polling_startup_consumes_the_pending_update_setting(monkeypatch, drop):
    import bot.main as app
    from bot.config import Settings

    settings = Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        RUN_MODE="polling",
        POLLING_DROP_PENDING_UPDATES=drop,
    )
    monkeypatch.setattr(app, "settings", settings)
    monkeypatch.setattr(app, "setup_bot_commands", AsyncMock())
    monkeypatch.setattr(app, "setup_scheduler", lambda **_kwargs: None)
    monkeypatch.setattr(app, "get_session_factory", lambda: None)
    bot = SimpleNamespace(delete_webhook=AsyncMock())
    await app.on_startup(bot)
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=drop)


def test_retry_keeps_credentials_and_hides_failed_probe_output(tmp_path):
    bundle = load_bundle(_bundle(tmp_path))
    runner = FakeRunner(
        [
            subprocess.CompletedProcess([], 1, "private-token", "private-token"),
            subprocess.CompletedProcess([], 0, '{"ok":true,"username":"test_bot"}'),
        ]
    )
    output = []
    installer = Installer(
        bundle.root,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: "1",
        output=output.append,
    )
    assert (
        installer._checked_probe(bundle, "telegram", {"token": "private-token"})["username"]
        == "test_bot"
    )
    assert [json.loads(call["input"])["token"] for call in runner.calls] == [
        "private-token",
        "private-token",
    ]
    assert "private-token" not in "\n".join(output)


def test_compose_validation_failure_keeps_target_absent(tmp_path):
    bundle = load_bundle(_bundle(tmp_path))
    target = tmp_path / "install"
    runner = FakeRunner([subprocess.CompletedProcess([], 1, "", "private-token")])
    installer = Installer(bundle.root, target, runner=runner)
    with pytest.raises(InstallerError, match="Compose"):
        installer._install(bundle, {"BOT_TOKEN": "private-token"})
    assert not target.exists()
    assert not list(tmp_path.glob(".neo-test-stage-*"))


def test_cli_eof_exits_without_traceback_or_starting_setup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["/bin/sh", str(root / "install.sh")],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 130
    assert "NEO" in result.stdout and "Choose an action" in result.stdout
    assert "Traceback" not in result.stderr
    assert "cancelled" in result.stderr.lower()


def test_webhook_is_left_untouched_until_the_user_starts_services(tmp_path):
    bundle = load_bundle(_bundle(tmp_path))
    answers = iter(
        ["test_bot", "1", "42", "", "https://panel.example", "test.example.com", "8.8.8.8", "n"]
    )
    secrets = iter(["private-token", "private-panel"])
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        result = (
            {"ok": True, "username": "test_bot", "has_webhook": True}
            if args[-1] == "telegram"
            else {"ok": True}
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(result))

    installer = Installer(
        bundle.root,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: next(answers),
        secret_prompt=lambda _label: next(secrets),
        output=lambda _message: None,
    )
    installer._collect_settings(bundle)
    assert [args[-1] for args in calls] == ["telegram", "panel"]


def test_start_time_webhook_switch_is_explicit_and_checked(tmp_path):
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"username":"test_bot","has_webhook":true}'
            ),
            subprocess.CompletedProcess(
                [], 0, '{"ok":true,"username":"test_bot","has_webhook":false}'
            ),
        ]
    )
    installer = Installer(
        tmp_path,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: "y",
        output=lambda _message: None,
    )
    installer._prepare_polling()
    assert [call["args"][-1] for call in runner.calls] == [
        "telegram-config",
        "telegram-config-remove-webhook",
    ]
    assert json.loads(runner.calls[1]["input"]) == {"expected_username": "test_bot"}


def test_declining_webhook_switch_performs_no_mutation(tmp_path):
    runner = FakeRunner(
        [subprocess.CompletedProcess([], 0, '{"ok":true,"username":"test_bot","has_webhook":true}')]
    )
    installer = Installer(
        tmp_path,
        tmp_path / "install",
        runner=runner,
        prompt=lambda _label: "n",
        output=lambda _message: None,
    )
    assert installer._prepare_polling() is False
    assert len(runner.calls) == 1
    assert runner.calls[0]["args"][-1] == "telegram-config"


@pytest.mark.asyncio
async def test_saved_environment_probe_checks_bot_identity_without_mutation(monkeypatch):
    import aiogram

    monkeypatch.setenv("BOT_TOKEN", "private-token")
    monkeypatch.setenv("BOT_USERNAME", "test_bot")
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="private")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(aiogram, "Bot", lambda **_kwargs: bot)
    result = await install_probe._dispatch("telegram-config", {})
    assert result == {"ok": True, "username": "test_bot", "has_webhook": True}
    bot.delete_webhook.assert_not_awaited()
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_environment_refuses_removal_for_a_different_confirmation(monkeypatch):
    import aiogram

    monkeypatch.setenv("BOT_TOKEN", "private-token")
    monkeypatch.setenv("BOT_USERNAME", "test_bot")
    monkeypatch.setattr(
        aiogram,
        "Bot",
        lambda **_kwargs: pytest.fail(
            "Wrong bot confirmation must be rejected before a network call"
        ),
    )
    assert (
        await install_probe._dispatch(
            "telegram-config-remove-webhook", {"expected_username": "other_bot"}
        )
    )["ok"] is False


def test_quoted_ubuntu_id_is_supported(monkeypatch):
    from scripts import test_installer

    monkeypatch.setattr(test_installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(test_installer.sys, "platform", "linux")
    host = SimpleNamespace(machine="x86_64")
    monkeypatch.setattr(test_installer.os, "uname", lambda: host)
    original = Path.read_text

    def os_release(path, *args, **kwargs):
        if str(path) == "/etc/os-release":
            return 'ID="ubuntu"\nVERSION_ID="24.04"\n'
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", os_release)
    test_installer._default_host_check()
