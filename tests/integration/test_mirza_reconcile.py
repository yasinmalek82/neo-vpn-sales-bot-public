"""Frozen-cutover verification checks individual identities, money and source maps."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from database.models import (
    MirzaBatch,
    MirzaRecord,
    PasarGuardAccount,
    ReportOutbox,
    User,
    WalletLedgerEntry,
)
from services.mirza_import import PanelBinding, apply_migration, plan_migration
from tests.integration.test_mirza_import import policy, source


async def imported(factory):
    rules = replace(
        policy(),
        accept_quarantine=frozenset({"Payment_report:p-1"}),
        panel_bindings=(
            PanelBinding("old-1", 41, "legacy_exact", "https://panel.invalid/sub/reviewed", True),
        ),
    )
    plan = plan_migration(source(), rules)
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
    return plan


async def test_verify_is_read_only_and_checks_the_complete_import(two_sessions):
    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        before = await session.scalar(select(func.count()).select_from(ReportOutbox))
        result = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert result.ok
        public = result.document()
        assert public["observed_wallet_total_toman"] == 125000
        assert public["checked_users"] == 2 and public["checked_records"] == 5
        assert not session.new and not session.dirty and not session.deleted
        assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == before


@pytest.mark.parametrize(
    "damage,issue",
    [
        ("swapped_balances", "wallet_mismatch"),
        ("ledger", "opening_credit_mismatch"),
        ("payload", "source_record_mismatch"),
        ("owner", "account_binding_mismatch"),
        ("disposition", "source_record_mismatch"),
        ("opening_reference", "opening_reference_mismatch"),
        ("unit", "batch_mismatch"),
    ],
)
async def test_verify_detects_drift_without_repairing_it(two_sessions, damage, issue):
    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        users = list((await session.scalars(select(User).order_by(User.telegram_id))).all())
        record = await session.get(MirzaRecord, (plan.policy.namespace, "user", "1001"))
        if damage == "swapped_balances":
            users[0].wallet_balance_toman, users[1].wallet_balance_toman = 0, 125000
        elif damage == "ledger":
            entry = await session.scalar(select(WalletLedgerEntry))
            entry.amount_toman -= 1
        elif damage == "payload":
            record.payload = {**record.payload, "Balance": "different"}
        elif damage == "owner":
            account = await session.scalar(select(PasarGuardAccount))
            account.user_id = users[1].id
        elif damage == "disposition":
            record.disposition = "history"
        elif damage == "opening_reference":
            record.opening_entry_id = None
        else:
            batch = await session.get(MirzaBatch, plan.policy.namespace)
            batch.source_unit = "rial"
        await session.commit()
        result = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert not result.ok and issue in result.document()["issue_counts"]
        assert not session.new and not session.dirty and not session.deleted
        if damage == "swapped_balances":
            assert result.document()["observed_wallet_total_toman"] == 125000
            assert [u.wallet_balance_toman for u in users] == [0, 125000]


async def test_unapplied_namespace_never_claims_zero_is_a_verified_balance(two_sessions):
    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = plan_migration(source(), policy())
    async with factory() as session:
        result = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert not result.ok
        assert result.document()["observed_wallet_total_toman"] is None
        assert result.document()["issue_counts"] == {"batch_missing": 1}


async def test_verify_target_enforces_database_read_only(two_sessions, monkeypatch):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from scripts import migrate_mirza

    factory, url = two_sessions
    plan = await imported(factory)

    async def unexpected_write(session, _plan, *, confirm_sha256):
        assert confirm_sha256 == plan.sha256
        await session.execute(update(User).values(wallet_balance_toman=1))
        raise AssertionError("database_permitted_write")

    monkeypatch.setattr(migrate_mirza, "reconcile_migration", unexpected_write, raising=False)
    with pytest.raises(DBAPIError):
        await migrate_mirza._verify(plan, url, plan.sha256)
    async with factory() as session:
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.telegram_id == 1001))
            == 125000
        )


async def test_verify_never_creates_a_missing_sqlite_target(tmp_path):
    from scripts import migrate_mirza
    from services.mirza_import import MigrationError

    target = tmp_path / "missing.db"
    plan = plan_migration(source(), policy())
    with pytest.raises(MigrationError, match="verify_target_missing"):
        await migrate_mirza._verify(plan, f"sqlite+aiosqlite:///{target}", plan.sha256)
    assert not target.exists()


async def test_verify_does_not_contact_the_panel(two_sessions, monkeypatch):
    from scripts import migrate_mirza

    factory, url = two_sessions
    plan = await imported(factory)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("read_only_verification_contacted_panel")

    monkeypatch.setattr(migrate_mirza, "get_pasarguard_client", forbidden)
    assert (await migrate_mirza._verify(plan, url, plan.sha256)).ok


@pytest.mark.parametrize("damage", [False, True])
async def test_operator_verify_command_returns_mismatch_exit_and_private_details(
    two_sessions, tmp_path, damage
):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from services.mirza_source import read_mirza_dump
    from tests.unit.test_mirza_cli import files

    factory, url = two_sessions
    source_file, policy_file = files(tmp_path)
    from scripts.migrate_mirza import _policy

    plan = plan_migration(read_mirza_dump(source_file.read_bytes()), _policy(policy_file))
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        if damage:
            user = await session.scalar(select(User))
            user.wallet_balance_toman += 1
        await session.commit()
    report = tmp_path / "verification.json"
    script = Path(__file__).resolve().parents[2] / "scripts/migrate_mirza.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(source_file),
            "--policy",
            str(policy_file),
            "--verify",
            "--source-sha256",
            plan.source_sha256,
            "--confirm-plan-sha256",
            plan.sha256,
            "--target-url-env",
            "NEO_VERIFY_TARGET_URL",
            "--output",
            str(report),
        ],
        cwd=tmp_path,
        env={**os.environ, "NEO_VERIFY_TARGET_URL": url},
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )
    assert result.returncode == (3 if damage else 0), result.stderr
    summary = json.loads(result.stdout)
    assert summary["verified"] is not damage
    assert "private_customer" not in result.stdout + result.stderr
    assert "source_key" not in result.stdout
    assert report.stat().st_mode & 0o777 == 0o600
    detail = json.loads(report.read_text())
    assert bool(detail["issues"]) is damage


@pytest.mark.parametrize("mark", ["deleted_at", "delete_requested_at"])
async def test_deleted_or_pending_delete_binding_is_not_verified(two_sessions, mark):
    from datetime import UTC, datetime

    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        account = await session.scalar(select(PasarGuardAccount))
        setattr(account, mark, datetime.now(UTC))
        await session.commit()
        report = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert not report.ok
        assert "account_binding_mismatch" in report.document()["issue_counts"]
        assert getattr(account, mark) is not None


async def test_read_only_service_refuses_a_session_with_uncommitted_changes(two_sessions):
    from services.mirza_import import MigrationError
    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        user.wallet_balance_toman = 7
        with pytest.raises(MigrationError, match="uncommitted_changes"):
            await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert user.wallet_balance_toman == 7 and user in session.dirty
        await session.rollback()


async def test_equal_totals_after_extra_credit_and_debit_still_fail_freeze_gate(two_sessions):
    from services.mirza_reconcile import reconcile_migration
    from services.wallet import credit, debit

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        await credit(
            session, user_id=user.id, amount_toman=1, reason="test", idempotency_key="extra-credit"
        )
        await debit(
            session, user_id=user.id, amount_toman=1, reason="test", idempotency_key="extra-debit"
        )
        await session.commit()
        report = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert not report.ok and report.observed_wallet_total_toman == 125000
        assert "unexpected_ledger_entry" in report.document()["issue_counts"]


async def test_unreviewed_order_is_detected_even_without_a_wallet_delta(two_sessions):
    from database.models import Order, Plan
    from database.models.plan import DataLimitResetStrategy
    from services.mirza_reconcile import reconcile_migration

    factory, _ = two_sessions
    plan = await imported(factory)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        product = Plan(
            name="unexpected-order",
            data_limit_gb=1,
            duration_days=1,
            price_toman=1,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=0,
            is_active=True,
        )
        session.add(product)
        await session.flush()
        session.add(Order(user_id=user.id, plan_id=product.id, price_toman=1, discount_toman=0))
        await session.commit()
        report = await reconcile_migration(session, plan, confirm_sha256=plan.sha256)
        assert not report.ok and report.observed_wallet_total_toman == 125000
        assert "unexpected_orders" in report.document()["issue_counts"]
