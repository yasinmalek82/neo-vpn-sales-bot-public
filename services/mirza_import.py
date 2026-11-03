"""Preview and atomically import one immutable Mirza snapshot, without remote IO.

History is retained as source records, never replayed as sales, top-ups or rewards.
Only the explicitly reviewed final balance becomes one shared-wallet credit.
Panel bindings must be collected and verified before the write transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PasarGuardAccount, User
from database.models.mirza_import import MirzaBatch, MirzaRecord
from database.repositories.user import _generate_referral_code
from services.mirza_source import DumpError, MirzaDump
from services.wallet import credit

_KEYS = {
    "user": "id",
    "invoice": "id_invoice",
    "Payment_report": "id",
    "service_other": "id",
    "marzban_panel": "id",
}
_HISTORY_INVOICES = {
    "disabled",
    "removetime",
    "removevolume",
    "disabledn",
    "removebyadmin",
    "removedbyadmin",
    "removebyuser",
}


class MigrationError(ValueError):
    """Safe error code; caller must roll back on failure."""


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PanelBinding:
    invoice_id: str
    panel_user_id: int
    panel_username: str
    subscription_url: str | None = field(default=None, repr=False)
    delete_exempt: bool = False


@dataclass(frozen=True)
class MigrationPolicy:
    namespace: str
    source_unit: str | None
    archive_invoice_ids: frozenset[str] = frozenset()
    accept_quarantine: frozenset[str] = frozenset()
    panel_bindings: tuple[PanelBinding, ...] = ()


@dataclass(frozen=True)
class _SourceRecord:
    table: str
    key: str
    owner: int | None
    disposition: str
    payload: dict[str, str | None] = field(repr=False)


@dataclass(frozen=True)
class MigrationPlan:
    source_sha256: str
    policy: MigrationPolicy
    records: tuple[_SourceRecord, ...] = field(repr=False)
    users: tuple[dict[str, Any], ...] = field(repr=False)
    blockers: tuple[str, ...]

    @property
    def opening_total_toman(self) -> int | None:
        if any(user["opening_toman"] is None for user in self.users):
            return None
        return sum(user["opening_toman"] for user in self.users)

    @property
    def sha256(self) -> str:
        # Recompute so mutating nested input after review invalidates confirmation.
        return _hash(self.document(private=True))

    def document(self, *, private: bool = False) -> dict[str, Any]:
        policy = asdict(self.policy)
        policy["archive_invoice_ids"] = sorted(self.policy.archive_invoice_ids)
        policy["accept_quarantine"] = sorted(self.policy.accept_quarantine)
        if not private:
            for binding in policy["panel_bindings"]:
                binding.pop("subscription_url", None)
        return {
            "format_version": 1,
            "source_sha256": self.source_sha256,
            "policy": policy,
            "blockers": list(self.blockers),
            "opening_total_toman": self.opening_total_toman,
            "users": list(self.users),
            "records": [
                {
                    "table": row.table,
                    "key": row.key,
                    "owner": row.owner,
                    "disposition": row.disposition,
                    "row_sha256": _hash(row.payload),
                    **({"payload": row.payload} if private else {}),
                }
                for row in self.records
            ],
        }


def _positive_id(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise MigrationError("invalid_identity")
    number = int(value)
    if not 0 < number <= 9223372036854775807:
        raise MigrationError("invalid_identity")
    return number


def _source_user(
    row: dict[str, str | None],
    source: MirzaDump,
    policy: MigrationPolicy,
    identities: set[int],
) -> dict[str, Any]:
    identity = _positive_id(row.get("id"))
    if identity in identities:
        raise MigrationError("duplicate_user")
    role, status = row.get("agent"), row.get("User_Status")
    if role not in {"f", "n"} or status not in {"Active", "block"}:
        raise MigrationError("unknown_role_or_status")
    opening: int | None = source.money(
        row.get("Balance") or "", source_unit=policy.source_unit or "toman"
    )
    if policy.source_unit is None:
        # Validate source integer shape while leaving the conversion unknown.
        opening = None
    counter = row.get("limit_usertest")
    if counter is None or not re.fullmatch(r"-?[0-9]{1,10}", counter):
        raise MigrationError("trial_counter_required")
    username = row.get("username")
    if username in {None, "", "none", "None"}:
        username = None
    if username is not None and len(username) > 64:
        raise MigrationError("invalid_username")
    return {
        "telegram_id": identity,
        "username": username,
        "opening_toman": opening,
        "is_reseller": role == "n",
        "is_banned": status == "block",
        "legacy_trial_consumed": int(counter) <= 0,
    }


def plan_migration(source: MirzaDump, policy: MigrationPolicy) -> MigrationPlan:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", policy.namespace):
        raise MigrationError("invalid_namespace")
    if not re.fullmatch(r"[a-f0-9]{64}", source.sha256):
        raise MigrationError("invalid_source_hash")
    blockers: set[str] = set()
    if policy.source_unit not in {"toman", "rial"}:
        blockers.add("money_unit_required")
    users: list[dict[str, Any]] = []
    identities: set[int] = set()
    for row in source.tables.get("user", []):
        try:
            user = _source_user(row, source, policy, identities)
            users.append(user)
            identities.add(user["telegram_id"])
        except (MigrationError, DumpError) as exc:
            blockers.add(f"user:{row.get('id', '?')}:{exc}")
    bindings = {binding.invoice_id: binding for binding in policy.panel_bindings}
    if len(bindings) != len(policy.panel_bindings):
        blockers.add("duplicate_binding")
    if len({b.panel_user_id for b in bindings.values()}) != len(bindings) or len(
        {b.panel_username for b in bindings.values()}
    ) != len(bindings):
        blockers.add("duplicate_panel_identity")
    records: list[_SourceRecord] = []
    seen: set[tuple[str, str]] = set()
    review: set[str] = set()
    invoice_ids: set[str] = set()
    for table, primary in _KEYS.items():
        for row in source.tables.get(table, []):
            key = row.get(primary) or ""
            if not key or len(key) > 128 or (table, key) in seen:
                blockers.add(f"invalid_or_duplicate_key:{table}")
                continue
            seen.add((table, key))
            owner = None
            if table != "marzban_panel":
                try:
                    owner = _positive_id(row.get("id") if table == "user" else row.get("id_user"))
                except MigrationError:
                    blockers.add(f"missing_owner:{table}:{key}")
                if owner not in identities:
                    blockers.add(f"missing_owner:{table}:{key}")
            disposition = "user" if table == "user" else "history"
            if table == "invoice":
                invoice_ids.add(key)
                binding = bindings.get(key)
                if key in policy.archive_invoice_ids:
                    disposition = "archived"
                    if binding is not None:
                        blockers.add(f"archived_binding:{key}")
                elif binding is not None:
                    if (
                        binding.panel_username != row.get("username")
                        or binding.panel_username.casefold() == "mirza_sample"
                        or not 0 < binding.panel_user_id <= 2147483647
                        or not 0 < len(binding.panel_username) <= 64
                        or (row.get("Status") or "").casefold() not in {"active", "send_on_hold"}
                    ):
                        blockers.add(f"invalid_binding:{key}")
                    disposition = "linked"
                elif (row.get("Status") or "").casefold() not in _HISTORY_INVOICES:
                    disposition = "quarantined"
            elif table in {"Payment_report", "service_other"}:
                status = (
                    row.get("payment_Status") if table == "Payment_report" else row.get("status")
                )
                if (status or "").casefold() not in {
                    "paid",
                    "expire",
                    "expired",
                    "reject",
                    "rejected",
                }:
                    disposition = "quarantined"
            if disposition == "quarantined":
                ref = f"{table}:{key}"
                review.add(ref)
                if ref not in policy.accept_quarantine:
                    blockers.add(f"review:{ref}")
            records.append(_SourceRecord(table, key, owner, disposition, deepcopy(row)))
    if policy.archive_invoice_ids - invoice_ids:
        blockers.add("unknown_archive_ids")
    if bindings.keys() - invoice_ids:
        blockers.add("unknown_binding_ids")
    if policy.accept_quarantine - review:
        blockers.add("unknown_review_refs")
    return MigrationPlan(
        source.sha256, policy, tuple(records), tuple(users), tuple(sorted(blockers))
    )


async def _existing_batch(session: AsyncSession, plan: MigrationPlan) -> bool:
    batch = await session.get(MirzaBatch, plan.policy.namespace)
    if batch is None:
        return False
    if batch.source_sha256 != plan.source_sha256 or batch.plan_sha256 != plan.sha256:
        raise MigrationError("namespace_already_bound")
    if batch.source_unit not in {None, plan.policy.source_unit}:
        raise MigrationError("source_unit_conflict")
    rows = list(
        (
            await session.scalars(
                select(MirzaRecord).where(MirzaRecord.namespace == batch.namespace)
            )
        ).all()
    )
    expected = {(row.table, row.key): _hash(row.payload) for row in plan.records}
    if {(row.source_table, row.source_key): row.source_row_sha256 for row in rows} != expected:
        raise MigrationError("mapping_drift")
    return True


async def apply_migration(
    session: AsyncSession, plan: MigrationPlan, *, confirm_sha256: str
) -> bool:
    """Caller owns transaction; errors require rollback. No commit or remote IO here."""
    plan = deepcopy(plan)
    if confirm_sha256 != plan.sha256:
        raise MigrationError("confirmation_mismatch")
    if plan.blockers:
        raise MigrationError("plan_blocked")
    if await _existing_batch(session, plan):
        return False
    ids = [user["telegram_id"] for user in plan.users]
    if await session.scalar(select(User.id).where(User.telegram_id.in_(ids)).limit(1)) is not None:
        raise MigrationError("target_user_exists")
    await session.flush()
    try:
        async with session.begin_nested():
            session.add(
                MirzaBatch(
                    namespace=plan.policy.namespace,
                    source_sha256=plan.source_sha256,
                    source_unit=plan.policy.source_unit,
                    plan_sha256=plan.sha256,
                )
            )
            await session.flush()
    except IntegrityError:
        if await _existing_batch(session, plan):
            return False
        raise
    targets: dict[int, User] = {}
    entries: dict[int, int] = {}
    for source_user in plan.users:
        identity = source_user["telegram_id"]
        user = User(
            telegram_id=identity,
            username=source_user["username"],
            is_reseller=source_user["is_reseller"],
            is_banned=source_user["is_banned"],
            is_admin=False,
            referral_code=_generate_referral_code(),
            wallet_balance_toman=0,
            legacy_trial_consumed=source_user["legacy_trial_consumed"],
        )
        session.add(user)
        await session.flush()
        targets[identity] = user
        if source_user["opening_toman"]:
            entry, created = await credit(
                session,
                user_id=user.id,
                amount_toman=source_user["opening_toman"],
                reason="migration_opening",
                idempotency_key=f"mirza:{plan.policy.namespace}:opening:{identity}",
            )
            if not created:
                raise MigrationError("opening_key_collision")
            entries[identity] = entry.id
    bindings = {binding.invoice_id: binding for binding in plan.policy.panel_bindings}
    for row in plan.records:
        record_user = targets.get(row.owner) if row.owner is not None else None
        account_id = None
        if row.disposition == "linked":
            binding = bindings[row.key]
            taken = await session.scalar(
                select(PasarGuardAccount.id).where(
                    or_(
                        PasarGuardAccount.panel_user_id == binding.panel_user_id,
                        PasarGuardAccount.panel_username == binding.panel_username,
                    )
                )
            )
            if taken is not None:
                raise MigrationError("account_owner_conflict")
            assert record_user is not None
            account = PasarGuardAccount(
                user_id=record_user.id,
                panel_user_id=binding.panel_user_id,
                panel_username=binding.panel_username,
                subscription_url=binding.subscription_url,
                display_name=binding.panel_username,
                delete_exempt=binding.delete_exempt,
                note=f"mirza:{plan.policy.namespace}:{row.key}",
            )
            session.add(account)
            await session.flush()
            account_id = account.id
        session.add(
            MirzaRecord(
                namespace=plan.policy.namespace,
                source_table=row.table,
                source_key=row.key,
                source_row_sha256=_hash(row.payload),
                payload=row.payload,
                disposition=row.disposition,
                target_user_id=record_user.id if record_user else None,
                target_account_id=account_id,
                opening_entry_id=(
                    entries.get(row.owner)
                    if row.table == "user" and row.owner is not None
                    else None
                ),
            )
        )
    await session.flush()
    return True
