"""Read-only frozen-cutover reconciliation against one reviewed import plan.

Run before writers are released. Later legitimate sales or admin changes require
their own reconciliation; this command never treats them as an opening balance
or repairs a difference. The caller owns a consistent read-only transaction.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    MirzaBatch,
    MirzaRecord,
    Order,
    PasarGuardAccount,
    Payment,
    ReferralLog,
    User,
    WalletEntryType,
    WalletLedgerEntry,
)
from services.mirza_import import MigrationError, MigrationPlan, PanelBinding


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    source_table: str | None = None
    source_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Reconciliation:
    namespace: str
    source_sha256: str
    plan_sha256: str
    expected_users: int
    expected_records: int
    opening_total_toman: int | None
    checked_users: int
    checked_records: int
    observed_wallet_total_toman: int | None
    issues: tuple[ReconciliationIssue, ...] = field(repr=False)

    @property
    def ok(self) -> bool:
        return not self.issues

    def document(self, *, private: bool = False) -> dict[str, Any]:
        result = {key: value for key, value in asdict(self).items() if key != "issues"}
        result.update(
            verified=self.ok,
            issue_counts=dict(sorted(Counter(x.code for x in self.issues).items())),
        )
        if private:
            result["issues"] = [asdict(issue) for issue in self.issues]
        return result


def _check_user(
    user: User,
    expected: dict[str, Any],
    entries: list[WalletLedgerEntry],
    namespace: str,
    issues: list[ReconciliationIssue],
) -> int | None:
    key = str(expected["telegram_id"])

    def issue(code: str) -> None:
        issues.append(ReconciliationIssue(code, "user", key))

    amount = expected["opening_toman"]
    if user.wallet_balance_toman != amount:
        issue("wallet_mismatch")
    if any(
        getattr(user, name) != expected[name]
        for name in ("username", "is_reseller", "is_banned", "legacy_trial_consumed")
    ):
        issue("user_policy_mismatch")
    ledger_balance = sum(
        e.amount_toman if e.entry_type == WalletEntryType.credit else -e.amount_toman
        for e in entries
    )
    if ledger_balance != user.wallet_balance_toman:
        issue("ledger_balance_mismatch")
    expected_key = f"mirza:{namespace}:opening:{key}"
    opening = next((e for e in entries if e.idempotency_key == expected_key), None)
    if amount:
        if opening is None or (
            opening.entry_type != WalletEntryType.credit
            or opening.reason != "migration_opening"
            or opening.amount_toman != amount
            or opening.balance_after_toman != amount
            or opening.order_id is not None
        ):
            issue("opening_credit_mismatch")
    elif opening is not None:
        issue("unexpected_opening_credit")
    if len(entries) != int(bool(amount)) or any(e is not opening for e in entries):
        issue("unexpected_ledger_entry")
    return opening.id if opening else None


def _account_matches(
    account: PasarGuardAccount | None, binding: PanelBinding, user_id: int | None
) -> bool:
    return account is not None and (
        account.user_id == user_id
        and account.panel_user_id == binding.panel_user_id
        and account.panel_username == binding.panel_username
        and account.subscription_url == binding.subscription_url
        and account.delete_exempt == binding.delete_exempt
        and account.deleted_at is None
        and account.delete_requested_at is None
    )


async def reconcile_migration(
    session: AsyncSession, plan: MigrationPlan, *, confirm_sha256: str
) -> Reconciliation:
    plan = deepcopy(plan)
    if confirm_sha256 != plan.sha256:
        raise MigrationError("confirmation_mismatch")
    if plan.blockers:
        raise MigrationError("plan_blocked")
    if session.new or session.dirty or session.deleted:
        raise MigrationError("uncommitted_changes")
    issues: list[ReconciliationIssue] = []

    def result(users: int = 0, records: int = 0, total: int | None = None) -> Reconciliation:
        return Reconciliation(
            plan.policy.namespace,
            plan.source_sha256,
            plan.sha256,
            len(plan.users),
            len(plan.records),
            plan.opening_total_toman,
            users,
            records,
            total,
            tuple(issues),
        )

    batch = await session.get(MirzaBatch, plan.policy.namespace, populate_existing=True)
    if batch is None:
        issues.append(ReconciliationIssue("batch_missing"))
        return result()
    if (batch.source_sha256, batch.plan_sha256, batch.source_unit) != (
        plan.source_sha256,
        plan.sha256,
        plan.policy.source_unit,
    ):
        issues.append(ReconciliationIssue("batch_mismatch"))
        return result()
    users = {
        u.telegram_id: u
        for u in (
            await session.scalars(
                select(User)
                .where(User.telegram_id.in_([u["telegram_id"] for u in plan.users]))
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    user_ids = {u.id for u in users.values()}
    # Writers must still be frozen. An unpaid order or replayed historical
    # payment/reward can leave today's wallet total unchanged while violating
    # the reviewed import. Restrict every check to imported identities.
    for code, model, predicate in (
        ("unexpected_orders", Order, Order.user_id.in_(user_ids)),
        ("unexpected_payments", Payment, Payment.user_id.in_(user_ids)),
        (
            "unexpected_referrals",
            ReferralLog,
            or_(ReferralLog.referrer_id.in_(user_ids), ReferralLog.referred_id.in_(user_ids)),
        ),
    ):
        if await session.scalar(select(func.count()).select_from(model).where(predicate)):
            issues.append(ReconciliationIssue(code))
    entries: dict[int, list[WalletLedgerEntry]] = defaultdict(list)
    for entry in (
        await session.scalars(
            select(WalletLedgerEntry)
            .where(WalletLedgerEntry.user_id.in_(user_ids))
            .execution_options(populate_existing=True)
        )
    ).all():
        entries[entry.user_id].append(entry)
    openings = {}
    for expected in plan.users:
        user = users.get(expected["telegram_id"])
        if user is None:
            issues.append(ReconciliationIssue("user_missing", "user", str(expected["telegram_id"])))
            continue
        openings[user.id] = _check_user(
            user, expected, entries[user.id], plan.policy.namespace, issues
        )
    rows = {
        (r.source_table, r.source_key): r
        for r in (
            await session.scalars(
                select(MirzaRecord)
                .where(MirzaRecord.namespace == plan.policy.namespace)
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    linked_ids = {
        r.target_account_id
        for r in rows.values()
        if r.disposition == "linked" and r.target_account_id is not None
    }
    accounts = {
        a.id: a
        for a in (
            await session.scalars(
                select(PasarGuardAccount)
                .where(
                    or_(
                        PasarGuardAccount.user_id.in_(user_ids),
                        PasarGuardAccount.id.in_(linked_ids),
                    )
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    bindings = {b.invoice_id: b for b in plan.policy.panel_bindings}
    expected_rows = {(r["table"], r["key"]): r for r in plan.document(private=True)["records"]}
    for key in sorted(set(rows) | set(expected_rows)):
        row, expected_row = rows.get(key), expected_rows.get(key)
        if row is None or expected_row is None:
            issues.append(
                ReconciliationIssue(
                    "source_record_missing" if row is None else "unexpected_source_record", *key
                )
            )
            continue
        owner = users.get(expected_row["owner"])
        owner_id = owner.id if owner is not None else None
        if (
            row.source_row_sha256 != expected_row["row_sha256"]
            or row.payload != expected_row["payload"]
            or row.disposition != expected_row["disposition"]
            or row.target_user_id != owner_id
        ):
            issues.append(ReconciliationIssue("source_record_mismatch", *key))
        entry_id = openings.get(owner_id) if key[0] == "user" and owner_id is not None else None
        if row.opening_entry_id != entry_id:
            issues.append(ReconciliationIssue("opening_reference_mismatch", *key))
        if expected_row["disposition"] == "linked":
            if not _account_matches(
                accounts.get(row.target_account_id) if row.target_account_id is not None else None,
                bindings[key[1]],
                owner_id,
            ):
                issues.append(ReconciliationIssue("account_binding_mismatch", *key))
        elif row.target_account_id is not None:
            issues.append(ReconciliationIssue("unexpected_account_reference", *key))
    if any(a.id not in linked_ids for a in accounts.values()):
        issues.append(ReconciliationIssue("unexpected_account_mapping"))
    total = (
        sum(u.wallet_balance_toman for u in users.values())
        if len(users) == len(plan.users)
        else None
    )
    return result(len(users), len(rows), total)
