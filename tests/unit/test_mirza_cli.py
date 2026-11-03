"""The operator command defaults to offline preview and binds apply to that preview."""

import json
import os
import subprocess
import sys
from pathlib import Path


def files(tmp_path: Path):
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE `user` (\n `id` bigint,\n `Balance` text,\n `username` text,\n `agent` text,\n `User_Status` text,\n `limit_usertest` text\n);\n"
        "INSERT INTO `user` VALUES (1001,'1234','private_customer','f','Active','1');\n"
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"namespace": "qa-mirza", "source_unit": "toman"}))
    return source, policy


def test_default_command_is_offline_and_writes_private_review_file(tmp_path: Path):
    source, policy = files(tmp_path)
    report = tmp_path / "review.json"
    target = tmp_path / "must-not-exist.db"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_mirza.py",
            str(source),
            "--policy",
            str(policy),
            "--output",
            str(report),
        ],
        env={**os.environ, "NEO_MIGRATION_DATABASE_URL": f"sqlite+aiosqlite:///{target}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert "private_customer" not in result.stdout
    summary = json.loads(result.stdout)
    assert summary["opening_total_toman"] == 1234
    assert json.loads(report.read_text())["plan_sha256"] == summary["plan_sha256"]
    assert report.stat().st_mode & 0o777 == 0o600


def test_apply_refuses_a_different_digest_before_opening_target(tmp_path: Path):
    source, policy = files(tmp_path)
    target = tmp_path / "must-not-exist.db"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_mirza.py",
            str(source),
            "--policy",
            str(policy),
            "--apply",
            "--source-sha256",
            "0" * 64,
            "--confirm-plan-sha256",
            "0" * 64,
            "--target-url-env",
            "NEO_MIGRATION_DATABASE_URL",
        ],
        env={**os.environ, "NEO_MIGRATION_DATABASE_URL": f"sqlite+aiosqlite:///{target}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0
    assert not target.exists()
    assert "source_hash_mismatch" in result.stderr


def test_unknown_currency_keeps_inventory_visible_without_claiming_zero_balance(tmp_path: Path):
    source, policy = files(tmp_path)
    policy.write_text(json.dumps({"namespace": "qa-mirza", "source_unit": None}))
    result = subprocess.run(
        [sys.executable, "scripts/migrate_mirza.py", str(source), "--policy", str(policy)],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["users"] == 1
    assert summary["opening_total_toman"] is None
    assert summary["blocker_count"] == 1


def test_preview_ignores_dotenv_and_needs_no_telegram_token(tmp_path: Path):
    source, policy = files(tmp_path)
    canary = "canary-secret-must-not-be-read"
    (tmp_path / ".env").write_text(
        f"BOT_TOKEN={canary}\nPASARGUARD_TIMEOUT=invalid-canary-value\n"
        f"DATABASE_URL=sqlite+aiosqlite:///{tmp_path / 'customer.db'}\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BOT_TOKEN", "PASARGUARD_TIMEOUT", "NEO_DISABLE_DOTENV"}
    }
    script = Path(__file__).resolve().parents[2] / "scripts/migrate_mirza.py"
    result = subprocess.run(
        [sys.executable, str(script), str(source), "--policy", str(policy)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["opening_total_toman"] == 1234
    assert canary not in result.stdout + result.stderr
    assert not (tmp_path / "customer.db").exists()
