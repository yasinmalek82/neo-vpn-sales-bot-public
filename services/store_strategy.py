"""STORE_STRATEGY: discounts, purchase-step composer, version history restore.

Handlers stay thin. Edits apply only to draft StoreVersion rows.
``payment_correctness_locked`` / ``service_ownership_locked`` cannot be turned off.
Card payment numbers live in env / settings — not invented into store tables.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models.store import StoreVersion, StoreVersionStatus
from services.promo_redemptions import (
    COUNTED_FROM_KEY,
    RULE_KEYS,
    PromoRules,
    rules_from_entry,
)
from services.store_admin import (
    StoreAdminError,
    clone_version_as_draft,
    get_draft_version,
    get_or_create_draft,
    load_store_version,
    locked_sales_rules,
)
from services.store_config import DEFAULT_PURCHASE_STEPS, DEFAULT_SALES_RULES

# Frozen catalog of composable blocks matching current purchase FSM
# (bot/handlers/purchase.py + PurchaseStates). No free-form branching DSL.
PURCHASE_STEP_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "plan_type", "title_fa": "انتخاب گروه محصول"},
    # Optional; customer visibility is driven by category sales_flags.time_limit_filter_enabled
    # (category flag wins over this global step list — see services.store_catalog).
    {"id": "volume_time_limit", "title_fa": "فیلتر محدودیت زمانی (حجمی)"},
    {"id": "volume_or_users", "title_fa": "حجم یا تعداد کاربر"},
    {"id": "duration", "title_fa": "مدت سرویس"},
    {"id": "service_name", "title_fa": "نام سرویس"},
    {"id": "renew_mode", "title_fa": "تمدید یا خرید جدید"},
    {"id": "promo", "title_fa": "کد تخفیف"},
    {"id": "confirm", "title_fa": "تأیید خرید"},
    {"id": "receipt", "title_fa": "ارسال فیش"},
)

_CATALOG_IDS: frozenset[str] = frozenset(b["id"] for b in PURCHASE_STEP_CATALOG)
_CATALOG_TITLE: dict[str, str] = {b["id"]: b["title_fa"] for b in PURCHASE_STEP_CATALOG}

ALLOWED_DISCOUNT_COMBINE = frozenset({"best_of_promo_vs_extra", "sum"})
PROMO_CODE_RE = re.compile(r"^[A-Z0-9_]{2,32}$")
# A typed limit or expiry is capped so a stray digit cannot make a code last forever or overflow a date.
MAX_PROMO_LIMIT = 1_000_000
MAX_PROMO_DAYS = 3650


@dataclass(frozen=True)
class VersionHistoryRow:
    id: int
    version: int
    status: str
    notes: str | None
    published_at: datetime | None
    updated_at: datetime
    category_count: int
    package_count: int


def step_title_fa(block_id: str) -> str:
    return _CATALOG_TITLE.get(block_id, block_id)


def normalize_purchase_steps(raw: list[Any] | None) -> list[dict[str, Any]]:
    """Normalize steps to ``{block, enabled}``; unknown blocks kept but flagged."""
    source = raw if isinstance(raw, list) and raw else list(DEFAULT_PURCHASE_STEPS)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in source:
        if isinstance(item, str):
            block = item.strip()
            enabled = True
        elif isinstance(item, dict) and item.get("block"):
            block = str(item["block"]).strip()
            enabled = bool(item.get("enabled", True))
        else:
            continue
        if not block or block in seen:
            continue
        seen.add(block)
        out.append({"block": block, "enabled": enabled})
    if not out:
        out = [{"block": s["block"], "enabled": True} for s in DEFAULT_PURCHASE_STEPS]
    return out


def catalog_blocks_not_in_steps(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    present = {s["block"] for s in steps}
    return [b for b in PURCHASE_STEP_CATALOG if b["id"] not in present]


async def get_draft_purchase_steps(session: AsyncSession) -> list[dict[str, Any]]:
    draft = await get_or_create_draft(session)
    steps = normalize_purchase_steps(draft.purchase_steps)
    # Persist normalization so reorder indices stay stable.
    if steps != (draft.purchase_steps or []):
        draft.purchase_steps = steps
        draft.updated_at = datetime.now(UTC)
        await session.flush()
    return steps


async def reorder_purchase_step(
    session: AsyncSession, index: int, *, direction: str
) -> list[dict[str, Any]]:
    """Move step at ``index`` up or down. ``direction`` is ``up`` or ``down``."""
    draft = await get_or_create_draft(session)
    steps = normalize_purchase_steps(draft.purchase_steps)
    if index < 0 or index >= len(steps):
        raise StoreAdminError("شماره گام نامعتبر است.")
    if direction == "up":
        if index == 0:
            return steps
        steps[index - 1], steps[index] = steps[index], steps[index - 1]
    elif direction == "down":
        if index >= len(steps) - 1:
            return steps
        steps[index + 1], steps[index] = steps[index], steps[index + 1]
    else:
        raise StoreAdminError("جهت جابه‌جایی نامعتبر است.")
    draft.purchase_steps = steps
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return steps


async def toggle_purchase_step(session: AsyncSession, index: int) -> list[dict[str, Any]]:
    draft = await get_or_create_draft(session)
    steps = normalize_purchase_steps(draft.purchase_steps)
    if index < 0 or index >= len(steps):
        raise StoreAdminError("شماره گام نامعتبر است.")
    block = steps[index]["block"]
    # Core path blocks should stay on for v1 safety — still allow toggle but
    # forbid disabling confirm (payment correctness UX).
    if block == "confirm" and steps[index].get("enabled", True):
        raise StoreAdminError("گام تأیید خرید را نمی‌توان خاموش کرد.")
    steps[index]["enabled"] = not bool(steps[index].get("enabled", True))
    draft.purchase_steps = steps
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return steps


async def add_purchase_step_from_catalog(
    session: AsyncSession, block_id: str
) -> list[dict[str, Any]]:
    block_id = block_id.strip()
    if block_id not in _CATALOG_IDS:
        raise StoreAdminError("این گام در فهرست مجاز نیست.")
    draft = await get_or_create_draft(session)
    steps = normalize_purchase_steps(draft.purchase_steps)
    if any(s["block"] == block_id for s in steps):
        raise StoreAdminError("این گام از قبل در لیست است.")
    steps.append({"block": block_id, "enabled": True})
    draft.purchase_steps = steps
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return steps


async def remove_purchase_step(session: AsyncSession, index: int) -> list[dict[str, Any]]:
    draft = await get_or_create_draft(session)
    steps = normalize_purchase_steps(draft.purchase_steps)
    if index < 0 or index >= len(steps):
        raise StoreAdminError("شماره گام نامعتبر است.")
    block = steps[index]["block"]
    if block in {"plan_type", "confirm"}:
        raise StoreAdminError("این گام پایه را نمی‌توان حذف کرد.")
    if len(steps) <= 1:
        raise StoreAdminError("حداقل یک گام باید بماند.")
    del steps[index]
    draft.purchase_steps = steps
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return steps


def get_sales_rules_view(rules: dict[str, Any] | None) -> dict[str, Any]:
    return locked_sales_rules(rules if isinstance(rules, dict) else {})


async def update_draft_sales_rules(
    session: AsyncSession,
    *,
    discount_combine: str | None = None,
) -> dict[str, Any]:
    """Owner-editable strategy flags on draft. Locked invariants stay True."""
    draft = await get_or_create_draft(session)
    rules = get_sales_rules_view(draft.sales_rules if isinstance(draft.sales_rules, dict) else {})
    if discount_combine is not None:
        if discount_combine not in ALLOWED_DISCOUNT_COMBINE:
            raise StoreAdminError("حالت ترکیب تخفیف نامعتبر است.")
        rules["discount_combine"] = discount_combine
    # Re-apply locks. The renewal flags stay on because checkout does not read
    # them; confirmation is not optional.
    rules["payment_correctness_locked"] = True
    rules["service_ownership_locked"] = True
    # Ensure renew keys always present
    rules.setdefault("renew_show_current_price", True)
    rules.setdefault("renew_require_customer_confirm", True)
    rules.setdefault(
        "discount_combine",
        DEFAULT_SALES_RULES.get("discount_combine", "best_of_promo_vs_extra"),
    )
    draft.sales_rules = rules
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return rules


def list_promo_codes(pricing_document: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The codes of a pricing document, each with its usage rules (defaults for old shapes)."""
    doc = pricing_document if isinstance(pricing_document, dict) else {}
    raw = doc.get("promo_codes") or {}
    if not isinstance(raw, dict):
        return []
    rows: list[dict[str, Any]] = []
    for code, meta in sorted(raw.items(), key=lambda kv: str(kv[0])):
        if isinstance(meta, dict):
            percent = meta.get("percent", 0)
            active = bool(meta.get("active", True))
        else:
            percent = meta
            active = True
        rules = rules_from_entry(meta)
        rows.append(
            {
                "code": str(code).upper(),
                "percent": percent,
                "active": active,
                "first_purchase_only": rules.first_purchase_only,
                "per_user_limit": rules.per_user_limit,
                "total_limit": rules.total_limit,
                "expires_at": rules.expires_at,
                "counted_from": rules.counted_from,
            }
        )
    return rows


def check_promo_code_and_percent(code: str, percent: float) -> tuple[str, float]:
    """The code upper-cased and the percent as a float, or ``StoreAdminError`` in Persian."""
    cleaned = code.strip().upper()
    if not PROMO_CODE_RE.match(cleaned):
        raise StoreAdminError("کد تخفیف باید ۲ تا ۳۲ حرف انگلیسی/عدد/زیرخط باشد (مثال: WELCOME10).")
    try:
        pct = float(percent)
    except (TypeError, ValueError) as exc:
        raise StoreAdminError("درصد تخفیف نامعتبر است.") from exc
    # «nan» compares false both ways, so it would pass the range check below.
    if not math.isfinite(pct) or pct <= 0 or pct > 100:
        raise StoreAdminError("درصد تخفیف باید بین ۰ و ۱۰۰ باشد.")
    return cleaned, pct


def _check_promo_rules(rules: PromoRules) -> None:
    """A malformed rule can never be stored, so checkout never has to guess what it meant."""
    for limit in (rules.per_user_limit, rules.total_limit):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= MAX_PROMO_LIMIT
        ):
            raise StoreAdminError("تعداد استفاده باید یک عدد صحیح و بدون علامت منفی باشد.")
    expires = rules.expires_at
    if expires is not None and (not isinstance(expires, datetime) or expires.tzinfo is None):
        raise StoreAdminError("تاریخ انقضا باید زمان دقیق با منطقهٔ زمانی باشد.")


def _rule_keys(rules: PromoRules) -> dict[str, Any]:
    """Only what differs from «no limit»: an entry with no rules stays the two-key shape."""
    keys: dict[str, Any] = {}
    if rules.first_purchase_only:
        keys["first_purchase_only"] = True
    if rules.per_user_limit:
        keys["per_user_limit"] = rules.per_user_limit
    if rules.total_limit:
        keys["total_limit"] = rules.total_limit
    if rules.expires_at is not None:
        keys["expires_at"] = rules.expires_at.isoformat()
    return keys


async def upsert_draft_promo(
    session: AsyncSession,
    *,
    code: str,
    percent: float,
    active: bool = True,
    rules: PromoRules | None = None,
) -> list[dict[str, Any]]:
    """Add or replace a code in the draft.

    ``rules=None`` keeps the usage rules the code already has (a re-save of the percent must
    not drop them); a ``PromoRules`` replaces them, and the defaults clear them.

    A code not yet in the draft is stamped ``counted_from`` = now: its uses count from here, so
    deleting a code and creating it again starts from zero. A code that is there keeps its stamp
    whichever way it is saved (and stays without one if it never had one).
    """
    cleaned, pct = check_promo_code_and_percent(code, percent)
    if rules is not None:
        _check_promo_rules(rules)
    draft = await get_or_create_draft(session)
    doc = copy.deepcopy(draft.pricing_document or {})
    if not isinstance(doc, dict):
        doc = {}
    promos = dict(doc.get("promo_codes") or {})
    entry: dict[str, Any] = {"percent": pct, "active": bool(active)}
    previous = promos.get(cleaned)
    if rules is not None:
        entry.update(_rule_keys(rules))
    elif isinstance(previous, dict):
        entry.update({k: previous[k] for k in RULE_KEYS if k in previous})
    if cleaned not in promos:
        entry[COUNTED_FROM_KEY] = datetime.now(UTC).isoformat()
    elif isinstance(previous, dict) and COUNTED_FROM_KEY in previous:
        entry[COUNTED_FROM_KEY] = previous[COUNTED_FROM_KEY]
    promos[cleaned] = entry
    doc["promo_codes"] = promos
    draft.pricing_document = doc
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return list_promo_codes(doc)


async def delete_draft_promo(session: AsyncSession, code: str) -> list[dict[str, Any]]:
    cleaned = code.strip().upper()
    draft = await get_or_create_draft(session)
    doc = copy.deepcopy(draft.pricing_document or {})
    promos = dict(doc.get("promo_codes") or {})
    if cleaned not in promos:
        raise StoreAdminError("این کد تخفیف در پیش‌نویس نیست.")
    del promos[cleaned]
    doc["promo_codes"] = promos
    draft.pricing_document = doc
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return list_promo_codes(doc)


async def toggle_draft_promo_active(session: AsyncSession, code: str) -> list[dict[str, Any]]:
    cleaned = code.strip().upper()
    draft = await get_or_create_draft(session)
    doc = copy.deepcopy(draft.pricing_document or {})
    promos = dict(doc.get("promo_codes") or {})
    if cleaned not in promos:
        raise StoreAdminError("این کد تخفیف در پیش‌نویس نیست.")
    meta = promos[cleaned]
    if isinstance(meta, dict):
        meta = dict(meta)
        meta["active"] = not bool(meta.get("active", True))
        promos[cleaned] = meta
    else:
        promos[cleaned] = {"percent": meta, "active": False}
    doc["promo_codes"] = promos
    draft.pricing_document = doc
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    return list_promo_codes(doc)


@dataclass(frozen=True)
class ChangeEntry:
    """One edit the owner made, as they should read it back a week later."""

    version_id: int
    version: int
    description: str
    made_at: datetime
    is_current: bool
    # Undoing restores a whole snapshot, so anything done afterwards goes with
    # it. This is how many later edits that is — the screen must say so.
    undoes_also: int
    # Is there an earlier state to go back to at all?
    can_undo: bool


async def list_store_changes(session: AsyncSession, *, limit: int = 20) -> list[ChangeEntry]:
    """The change log, newest first.

    Built from the version chain rather than a separate table: each published
    version already *is* the state after one edit, and the version it replaced
    is the state before. A parallel audit table would be a second source of
    truth that could disagree with the catalog it describes.
    """
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.status.in_([StoreVersionStatus.published, StoreVersionStatus.archived]))
        .order_by(StoreVersion.version.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return []

    # Only the *oldest surviving* state has nothing before it. Everything else
    # is undoable — including the newest change, which is the one the owner
    # reaches for most often.
    oldest_version = min(v.version for v in rows)
    has_older = (
        await session.scalar(
            select(func.count())
            .select_from(StoreVersion)
            .where(
                StoreVersion.version < oldest_version,
                StoreVersion.status.in_(
                    [StoreVersionStatus.published, StoreVersionStatus.archived]
                ),
            )
        )
    ) or 0

    entries: list[ChangeEntry] = []
    for index, version in enumerate(rows):
        is_oldest = version.version == oldest_version
        entries.append(
            ChangeEntry(
                version_id=version.id,
                version=version.version,
                description=(version.notes or "").strip() or "تغییر در فروشگاه",
                made_at=version.published_at or version.updated_at,
                is_current=version.status == StoreVersionStatus.published,
                # Everything newer than this entry is also rolled back.
                undoes_also=index,
                can_undo=(not is_oldest) or has_older > 0,
            )
        )
    return entries


async def undo_store_change(session: AsyncSession, version_id: int) -> StoreVersion:
    """Put the catalog back to how it was *before* the given change.

    The entry the owner tapped describes the edit that produced ``version_id``,
    so going back means restoring its predecessor — the newest version below it.
    """
    target = await session.get(StoreVersion, version_id)
    if target is None:
        raise StoreAdminError("این تغییر دیگر در تاریخچه نیست.")
    previous = (
        await session.execute(
            select(StoreVersion)
            .where(
                StoreVersion.version < target.version,
                StoreVersion.status.in_(
                    [StoreVersionStatus.published, StoreVersionStatus.archived]
                ),
            )
            .order_by(StoreVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is None:
        raise StoreAdminError("این اولین وضعیت فروشگاه است؛ قبل‌تر از این چیزی نیست.")
    return await restore_version_to_draft(session, previous.id)


async def list_version_history(session: AsyncSession) -> list[VersionHistoryRow]:
    """List published + archived versions (newest first). Drafts excluded from restore list."""
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.status.in_([StoreVersionStatus.published, StoreVersionStatus.archived]))
        .options(
            selectinload(StoreVersion.categories),
            selectinload(StoreVersion.packages),
            selectinload(StoreVersion.package_prices),
        )
        .order_by(StoreVersion.version.desc())
    )
    result = await session.execute(stmt)
    rows: list[VersionHistoryRow] = []
    for v in result.scalars().all():
        status = v.status.value if isinstance(v.status, StoreVersionStatus) else str(v.status)
        rows.append(
            VersionHistoryRow(
                id=v.id,
                version=v.version,
                status=status,
                notes=v.notes,
                published_at=v.published_at,
                updated_at=v.updated_at,
                category_count=len(v.categories or []),
                package_count=len(v.packages or []),
            )
        )
    return rows


async def restore_version_to_draft(session: AsyncSession, version_id: int) -> StoreVersion:
    """Deep-clone archived/published snapshot into a new draft. Never auto-publishes.

    If a draft already exists, it is discarded (deleted) so the restored snapshot
    becomes the sole draft — published row is never destroyed.
    """
    stmt = (
        select(StoreVersion)
        .where(StoreVersion.id == version_id)
        .options(
            selectinload(StoreVersion.categories),
            selectinload(StoreVersion.packages),
            selectinload(StoreVersion.package_prices),
        )
    )
    result = await session.execute(stmt)
    source = result.scalar_one_or_none()
    if source is None:
        raise StoreAdminError("نسخه یافت نشد.")
    status = source.status
    if status not in (StoreVersionStatus.published, StoreVersionStatus.archived):
        raise StoreAdminError("فقط نسخهٔ فعال یا بایگانی‌شده را می‌توان به پیش‌نویس برگرداند.")

    existing_draft = await get_draft_version(session)
    if existing_draft is not None:
        await session.delete(existing_draft)
        await session.flush()

    draft = await clone_version_as_draft(session, source)
    draft.notes = f"restore from v{source.version} (id={source.id})"
    draft.source = "admin_restore"
    # Ensure strategy defaults present after restore
    draft.sales_rules = locked_sales_rules(
        draft.sales_rules if isinstance(draft.sales_rules, dict) else {}
    )
    draft.sales_rules.setdefault("renew_show_current_price", True)
    draft.sales_rules.setdefault("renew_require_customer_confirm", True)
    draft.purchase_steps = normalize_purchase_steps(draft.purchase_steps)
    draft.updated_at = datetime.now(UTC)
    await session.flush()
    loaded = await load_store_version(session, draft.id)
    assert loaded is not None
    return loaded


__all__ = [
    "PURCHASE_STEP_CATALOG",
    "VersionHistoryRow",
    "step_title_fa",
    "normalize_purchase_steps",
    "catalog_blocks_not_in_steps",
    "get_draft_purchase_steps",
    "reorder_purchase_step",
    "toggle_purchase_step",
    "add_purchase_step_from_catalog",
    "remove_purchase_step",
    "get_sales_rules_view",
    "update_draft_sales_rules",
    "list_promo_codes",
    "upsert_draft_promo",
    "delete_draft_promo",
    "toggle_draft_promo_active",
    "check_promo_code_and_percent",
    "MAX_PROMO_LIMIT",
    "MAX_PROMO_DAYS",
    "list_version_history",
    "restore_version_to_draft",
]
