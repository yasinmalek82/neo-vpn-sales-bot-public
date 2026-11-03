"""Independent observer: durable transitions, safe SSH, and confirmed notification ACKs."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

IMAGE = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64


def monitor():
    assert (
        importlib.util.find_spec("scripts.monitor_runtime") is not None
    ), "external monitor missing"
    from scripts import monitor_runtime

    return monitor_runtime


def observation(**changes):
    return dict(
        id=CONTAINER_ID, image=IMAGE, running=True, status="running", health="healthy", **changes
    )


def notifier(module, responses=None):
    client = module.TelegramNotifier("123456:synthetic_secret", 123456, 560110047)
    answers = responses or [
        {"id": 123456, "is_bot": True},
        {"id": 560110047, "type": "private"},
        {"message_id": 91, "chat": {"id": 560110047, "type": "private"}},
        {"message_id": 92, "chat": {"id": 560110047, "type": "private"}},
    ]
    client.call = Mock(side_effect=answers)
    return client


def test_transition_queue_preserves_failed_failure_then_recovery_and_retries_in_order(tmp_path):
    module = monitor()
    path = tmp_path / "state.json"
    assert module.run_once(path, lambda: "healthy")["pending"] == 0
    broken = notifier(module, [OSError("secret token and URL must remain private")])
    result = module.run_once(path, lambda: "unreachable", broken)
    assert result["pending"] == 1 and result["notification"] == "failed"
    assert module.run_once(path, lambda: "unreachable")["pending"] == 1
    assert module.run_once(path, lambda: "unhealthy")["pending"] == 1
    assert module.run_once(path, lambda: "healthy")["pending"] == 2
    healthy_client = notifier(module)
    result = module.run_once(path, lambda: "healthy", healthy_client)
    assert result["pending"] == 0 and result["delivered"] == 2
    sends = [
        args.args[1]["text"]
        for args in healthy_client.call.call_args_list
        if args.args[0] == "sendMessage"
    ]
    assert "event=failure" in sends[0] and "reason=unreachable" in sends[0]
    assert "event=recovery" in sends[1]
    assert all(body.isascii() for body in sends)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "synthetic_secret" not in path.read_text()
    healthy_client.call.reset_mock()
    module.run_once(path, lambda: "healthy", healthy_client)
    healthy_client.call.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        [{"id": 999, "is_bot": True}],
        [{"id": 123456, "is_bot": True}, {"id": 560110047, "type": "group"}],
        [{"id": 123456, "is_bot": True}, {"id": 9, "type": "private"}],
        [
            {"id": 123456, "is_bot": True},
            {"id": 560110047, "type": "private"},
            {"message_id": 91, "chat": {"id": 9, "type": "private"}},
        ],
    ],
)
def test_identity_or_send_confirmation_mismatch_never_acknowledges(tmp_path, response):
    module = monitor()
    path = tmp_path / "state.json"
    result = module.run_once(path, lambda: "unhealthy", notifier(module, response))
    assert result["pending"] == 1
    assert result["notification"] == "failed"


def test_destination_binding_refuses_route_change_without_losing_pending(tmp_path):
    module = monitor()
    path = tmp_path / "state.json"
    original = notifier(module, [OSError("unavailable")])
    module.run_once(path, lambda: "unreachable", original)
    before = path.read_bytes()
    other = module.TelegramNotifier("123456:synthetic_secret", 123456, 999)
    with pytest.raises(module.MonitorError, match="route_mismatch"):
        module.run_once(path, lambda: "healthy", other)
    assert path.read_bytes() == before


def test_malformed_state_is_not_overwritten_or_leaked(tmp_path):
    module = monitor()
    path = tmp_path / "state.json"
    path.write_text('{"token":"private-do-not-print"}')
    with pytest.raises(module.MonitorError, match="state_invalid"):
        module.run_once(path, lambda: "healthy")
    assert path.read_text() == '{"token":"private-do-not-print"}'


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"image": "sha256:" + "c" * 64}, "image_mismatch"),
        ({"running": False, "status": "exited"}, "stopped"),
        ({"health": "unhealthy"}, "unhealthy"),
        ({"health": None}, "no_health"),
        ({"health": "starting"}, "unhealthy"),
        ({"running": "true"}, "malformed"),
        ({"id": ""}, "malformed"),
    ],
)
def test_docker_observation_fails_closed(changes, reason):
    module = monitor()
    payload = observation()
    payload.update(changes)
    assert module.assess(payload, IMAGE) == reason
    assert module.assess(observation(), IMAGE) == "healthy"
    assert module.assess({"private_stdout": "secret"}, IMAGE) == "malformed"


def test_ssh_only_reads_narrow_template_and_uses_explicit_sudo_opt_in(tmp_path, monkeypatch):
    module = monitor()
    key, hosts = tmp_path / "key", tmp_path / "known_hosts"
    key.touch()
    hosts.touch()
    config = module.SSHConfig("observer@host.example", "neo-bot", key, hosts, sudo=True)
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(observation()), stderr="secret"
        )
    )
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.observe_ssh(config, IMAGE) == "healthy"
    args, kwargs = run.call_args
    command = args[0]
    assert command[0] == "ssh"
    assert "-F" in command and "/dev/null" in command
    assert "StrictHostKeyChecking=yes" in command and "IdentitiesOnly=yes" in command
    assert command[-2] == "observer@host.example"
    import shlex

    remote = shlex.split(command[-1])
    assert remote[:4] == ["sudo", "-n", "docker", "inspect"]
    assert remote[-2:] == ["--", "neo-bot"]
    template = remote[remote.index("--format") + 1]
    assert '(index .State "Health")' in template and '(index . "Status")' in template
    assert "Config.Env" not in command[-1] and ".State.Health.Log" not in command[-1]
    assert kwargs["timeout"] <= 20 and kwargs.get("shell", False) is False
    run.return_value.returncode = 255
    assert module.observe_ssh(config, IMAGE) == "unreachable"
    run.return_value.returncode = 1
    assert module.observe_ssh(config, IMAGE) == "inspect_failed"
    run.side_effect = subprocess.TimeoutExpired("ssh secret", 15, stderr="secret")
    assert module.observe_ssh(config, IMAGE) == "unreachable"


@pytest.mark.parametrize(
    ("target", "container"),
    [
        ("-oProxyCommand=touch /tmp/pwn", "neo"),
        ("u@host;touch /tmp/pwn", "neo"),
        ("u@host", "--help"),
        ("u@host", "neo;reboot"),
        ("u@host", "neo\nreboot"),
    ],
)
def test_command_injection_rejected_before_subprocess(tmp_path, target, container):
    module = monitor()
    key, hosts = tmp_path / "key", tmp_path / "known_hosts"
    key.touch()
    hosts.touch()
    with pytest.raises(module.MonitorError, match="configuration_invalid"):
        module.SSHConfig(target, container, key, hosts).command()


def test_lock_serializes_observation_and_preserves_transition_order(tmp_path):
    module = monitor()
    path = tmp_path / "state.json"
    first_entered, release_first, second_entered = Event(), Event(), Event()

    def failing_observation():
        first_entered.set()
        assert release_first.wait(3)
        return "unreachable"

    def recovering_observation():
        second_entered.set()
        return "healthy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.run_once, path, failing_observation)
        assert first_entered.wait(3)
        second = pool.submit(module.run_once, path, recovering_observation)
        assert not second_entered.wait(0.05)
        release_first.set()
        assert first.result(timeout=3)["pending"] == 1
        assert second.result(timeout=3)["pending"] == 2
    state = json.loads(path.read_text())
    assert [event["kind"] for event in state["pending"]] == ["failure", "recovery"]


def test_cli_default_offline_fixture_and_notification_error_do_not_leak(
    tmp_path, monkeypatch, capsys
):
    module = monitor()
    monkeypatch.setattr(
        module.subprocess, "run", Mock(side_effect=AssertionError("network prohibited"))
    )
    assert module.main([]) == 0
    assert "not_requested" in capsys.readouterr().out
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(observation()))
    args = [
        "--fixture",
        str(fixture),
        "--expected-image",
        IMAGE,
        "--state",
        str(tmp_path / "state.json"),
    ]
    assert module.main(args) == 0
    monkeypatch.setenv("NEO_MONITOR_BOT_TOKEN", "123456:private-secret")
    monkeypatch.setenv("NEO_MONITOR_BOT_ID", "123456")
    monkeypatch.setenv("NEO_MONITOR_CHAT_ID", "560110047")
    monkeypatch.setattr(
        module.TelegramNotifier,
        "call",
        Mock(side_effect=OSError("https://api.telegram.org/bot123456:private-secret")),
    )
    fixture.write_text("malformed private-stdout")
    assert module.main([*args, "--notify"]) == 2
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err
    assert "private-stdout" not in output.out + output.err


@pytest.mark.parametrize("field", ["status", "health"])
def test_unhashable_observation_values_are_malformed(field):
    module = monitor()
    payload = observation()
    payload[field] = ["private malformed value"]
    assert module.assess(payload, IMAGE) == "malformed"


def test_ack_storage_failure_does_not_report_pending_event_as_acknowledged(tmp_path, monkeypatch):
    module = monitor()
    path = tmp_path / "state.json"
    save = module._save_state
    calls = []

    def fail_ack(path, state):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("private disk detail")
        save(path, state)

    monkeypatch.setattr(module, "_save_state", fail_ack)
    result = module.run_once(path, lambda: "unhealthy", notifier(module))
    assert result["notification"] == "failed"
    assert result["pending"] == 1
    assert result["delivered"] == 0
    assert len(json.loads(path.read_text())["pending"]) == 1


def test_ssh_known_hosts_filename_cannot_introduce_ssh_config_tokens(tmp_path):
    module = monitor()
    key, hosts = tmp_path / "key", tmp_path / "known\nhosts"
    key.touch()
    hosts.touch()
    with pytest.raises(module.MonitorError, match="configuration_invalid"):
        module.SSHConfig("u@host", "neo", key, hosts).command()


def test_actual_http_boundary_uses_tls_fixed_host_no_proxy_and_no_redirects(monkeypatch):
    import ssl

    module = monitor()
    response = Mock()
    response.status = 200
    response.read.return_value = b'{"ok":true,"result":{"id":123456,"is_bot":true}}'
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    build = Mock(return_value=opener)
    monkeypatch.setattr(module.urllib.request, "build_opener", build)
    client = module.TelegramNotifier("123456:synthetic_secret", 123456, 560110047)
    assert client.call("getMe", {}) == {"id": 123456, "is_bot": True}
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.telegram.org/bot123456:synthetic_secret/getMe"
    assert request.method == "POST"
    assert opener.open.call_args.kwargs["timeout"] == 10
    handlers = build.call_args.args
    assert handlers[0].proxies == {}
    assert handlers[1]._context.verify_mode == ssl.CERT_REQUIRED
    assert handlers[1]._context.check_hostname
    assert (
        handlers[2].redirect_request(None, None, 302, "private", {}, "https://foreign.test") is None
    )
    response.read.return_value = b'{"ok":false,"description":"private token/body"}'
    with pytest.raises(module.MonitorError, match="notification_failed"):
        client.call("getMe", {})


def test_invalid_expected_image_is_rejected_without_probe_or_state_write(tmp_path, monkeypatch):
    module = monitor()
    run = Mock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(module.subprocess, "run", run)
    path = tmp_path / "state.json"
    assert module.main(["--probe", "--expected-image", "latest;reboot", "--state", str(path)]) == 2
    assert not path.exists()
    run.assert_not_called()
