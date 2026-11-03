"""Effective PasarGuard config: DB overrides env (API key auth only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.client import PasarGuardClient
from api_client.exceptions import (
    PasarGuardAuthError,
    PasarGuardError,
    PasarGuardNotFoundError,
)
from bot.config import Settings
from database.models.pasarguard_settings import (
    PASARGUARD_SETTINGS_ROW_ID,
    PasarGuardSettings,
)
from services.pricing import PlanType

logger = logging.getLogger(__name__)

# base_url, api_key, sub_path, timeout (username/password unused)
_FIELD_COUNT = 4


@dataclass(frozen=True)
class EffectivePasarGuardConfig:
    """Resolved panel connection settings. Secrets never appear in repr/str."""

    base_url: str
    api_key: str = field(default="", repr=False)
    sub_path: str = "sub"
    timeout: float = 30.0
    source: str = "env"  # "db" | "env" | "mixed"

    @property
    def uses_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def auth_mode(self) -> str:
        return "api_key"

    def __str__(self) -> str:
        key_mask = "••••" if self.uses_api_key else "—"
        return (
            f"EffectivePasarGuardConfig(base_url={self.base_url!r}, "
            f"api_key={key_mask!r}, "
            f"sub_path={self.sub_path!r}, timeout={self.timeout!r}, "
            f"source={self.source!r}, "
            f"auth_mode={self.auth_mode!r})"
        )


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


async def get_settings_row(session: AsyncSession) -> PasarGuardSettings | None:
    result = await session.execute(
        select(PasarGuardSettings).where(PasarGuardSettings.id == PASARGUARD_SETTINGS_ROW_ID)
    )
    return result.scalar_one_or_none()


async def get_effective_pasarguard_config(
    session: AsyncSession,
    settings: Settings,
) -> EffectivePasarGuardConfig:
    """DB field overrides env when set; missing DB fields fall back to env.

    Auth is API key only. admin_username / admin_password columns are ignored.
    """
    return resolve_pasarguard_config(settings, await get_settings_row(session))


def resolve_pasarguard_config(
    settings: Settings, row: PasarGuardSettings | None
) -> EffectivePasarGuardConfig:
    """Resolve one already-read row without keeping a DB transaction open."""
    base_url = settings.pasar_guard_base_url
    api_key = settings.pasar_guard_api_key or ""
    sub_path = settings.pasar_guard_sub_path
    timeout = settings.pasar_guard_timeout
    db_hits = 0

    if row is not None:
        if _nonempty(row.base_url) is not None:
            base_url = row.base_url.strip()  # type: ignore[union-attr]
            db_hits += 1
        if _nonempty(row.api_key) is not None:
            api_key = row.api_key  # type: ignore[assignment]
            db_hits += 1
        if _nonempty(row.sub_path) is not None:
            sub_path = row.sub_path.strip()  # type: ignore[union-attr]
            db_hits += 1
        if row.timeout is not None:
            timeout = float(row.timeout)
            db_hits += 1

    if db_hits == 0:
        source = "env"
    elif row is not None and db_hits >= _FIELD_COUNT:
        source = "db"
    else:
        source = "mixed"

    return EffectivePasarGuardConfig(
        base_url=base_url,
        api_key=api_key or "",
        sub_path=sub_path,
        timeout=timeout,
        source=source,
    )


async def _ensure_row(session: AsyncSession) -> PasarGuardSettings:
    row = await get_settings_row(session)
    if row is None:
        row = PasarGuardSettings(id=PASARGUARD_SETTINGS_ROW_ID)
        session.add(row)
        await session.flush()
    return row


async def save_pasarguard_settings(
    session: AsyncSession,
    *,
    admin_user_id: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    sub_path: str | None = None,
    timeout: float | None = None,
    clear_api_key: bool = False,
    clear_timeout: bool = False,
) -> PasarGuardSettings:
    """Upsert singleton settings. Pass only fields to update; None = leave unchanged.

    ``clear_*=True`` clears that stored override (fall back to env). These go
    through ``_ensure_row`` like every other write, so clearing works even when
    no settings row exists yet — the caller must not reach for the row itself.
    Empty string for a string field clears it (fall back to env).
    Legacy admin_username / admin_password columns are nulled (unused).
    """
    row = await _ensure_row(session)
    if base_url is not None:
        row.base_url = _nonempty(base_url)
    if clear_api_key:
        row.api_key = None
    elif api_key is not None:
        row.api_key = _nonempty(api_key)
    if sub_path is not None:
        row.sub_path = _nonempty(sub_path)
    if clear_timeout:
        row.timeout = None
    elif timeout is not None:
        row.timeout = float(timeout)
    # Password auth removed — keep columns unused / cleared.
    row.admin_username = None
    row.admin_password = None
    row.updated_at = datetime.now(UTC)
    row.updated_by_admin_id = admin_user_id
    await session.flush()
    return row


async def get_trial_hwid_limit(session: AsyncSession, settings: Settings) -> int:
    """How many devices a free trial may connect; stored value, then env.

    Size and length belong to each store group's trial. The device count is
    the one trial setting that stays global.
    """
    row = await get_settings_row(session)
    return int((row.trial_hwid_limit if row else None) or settings.trial_hwid_limit)


# Size and length belong to each store group's trial (sales_flags); the
# device count is the one trial setting left here.
TRIAL_FIELDS: dict[str, str] = {
    "trial_hwid": "hwid_limit",
}

# Same shape as store_admin's DIM_LIMITS, for the same reason: a price typed into
# the days prompt must not become a 150000-day trial.
TRIAL_LIMITS: dict[str, int] = {
    "hwid_limit": 100,
}


async def save_trial_shape(
    session: AsyncSession,
    *,
    admin_user_id: int | None = None,
    hwid_limit: int | None = None,
    clear: str | None = None,
) -> PasarGuardSettings:
    """Write the trial device limit, or clear it back to the env default.

    ``clear`` names the field to NULL out — a separate argument because ``None``
    already means «leave unchanged» here, exactly as it does in
    ``save_pasarguard_settings``.

    Goes through ``_ensure_row`` so the very first edit works with no settings
    row present. That is the trap that made «ذخیره شد» a lie for group ids and
    timeout: the write silently did nothing and still reported success.
    """
    row = await _ensure_row(session)
    if clear is not None:
        if clear not in TRIAL_LIMITS:
            raise ValueError(f"unknown trial field: {clear}")
        setattr(row, f"trial_{clear}", None)
    if hwid_limit is not None:
        row.trial_hwid_limit = int(hwid_limit)
    row.updated_at = datetime.now(UTC)
    row.updated_by_admin_id = admin_user_id
    await session.flush()
    return row


async def clear_to_env(session: AsyncSession, *, admin_user_id: int | None = None) -> None:
    """Remove DB overrides so env values are used again."""
    row = await get_settings_row(session)
    if row is None:
        return
    row.base_url = None
    row.admin_username = None
    row.admin_password = None
    row.api_key = None
    row.sub_path = None
    row.default_group_ids = None
    row.product_group_ids = None
    row.timeout = None
    row.trial_data_limit_gb = None
    row.trial_duration_days = None
    row.trial_hwid_limit = None
    row.updated_at = datetime.now(UTC)
    row.updated_by_admin_id = admin_user_id
    await session.flush()


def panel_error_reason_fa(exc: BaseException) -> str:  # noqa: PLR0911 — one line per failure kind
    """Why the panel could not be reached, as the owner reads it.

    Decided by the kind of failure, never by echoing ``str(exc)`` or the class
    name: the old text was either English or «PasarGuardAuthError», and a
    credential-looking message could ride along. Nothing from the exception
    text is returned except a panel phrase ``report_events`` already knows.
    """
    from services.report_events import known_panel_error_fa

    status = getattr(exc, "status_code", None)
    text = str(exc).casefold()
    if isinstance(exc, PasarGuardAuthError):
        return "پنل کلید API را قبول نکرد"
    if isinstance(exc, httpx.TimeoutException | TimeoutError) or "timeout" in text:
        return "پنل در زمان تعیین‌شده جواب نداد"
    if isinstance(exc, PasarGuardNotFoundError):
        return "آدرس پنل درست نیست (مسیر API پیدا نشد)"
    known = known_panel_error_fa(text)
    if known is not None:
        return known
    if isinstance(exc, httpx.TransportError | OSError):
        return "به پنل وصل نشد؛ آدرس پنل یا شبکه را بررسی کن"
    if isinstance(exc, PasarGuardError) and status is not None:
        kind = "خطای داخلی داد" if int(status) >= 500 else "درخواست را رد کرد"
        return f"پنل {kind} (کد {int(status)})"
    if isinstance(exc, ValueError):
        return "تنظیمات اتصال پنل کامل یا درست نیست"
    return "اتصال به پنل ناموفق بود (دلیل نامشخص)"


async def check_panel_connection(
    session: AsyncSession,
    settings: Settings,
    *,
    client: PasarGuardClient | None = None,
) -> tuple[bool, str]:
    """Connectivity check via GET /api/admin with API key. No secret leaks."""
    owns = client is None
    if client is None:
        from api_client.factory import create_pasarguard_client_from_config

        cfg = await get_effective_pasarguard_config(session, settings)
        if not cfg.uses_api_key:
            return False, "کلید API تنظیم نشده است"
        try:
            client = create_pasarguard_client_from_config(cfg)
        except ValueError as exc:
            return False, panel_error_reason_fa(exc)
    try:
        await client.get_current_admin()
        logger.info("PasarGuard connection test succeeded")
        return True, "ok"
    except PasarGuardAuthError as exc:
        logger.warning("PasarGuard connection test auth failed: %s", type(exc).__name__)
        return False, panel_error_reason_fa(exc)
    except PasarGuardError as exc:
        logger.warning("PasarGuard connection test failed: %s", type(exc).__name__)
        return False, panel_error_reason_fa(exc)
    except Exception as exc:
        logger.warning("PasarGuard connection test unexpected: %s", type(exc).__name__)
        return False, panel_error_reason_fa(exc)
    finally:
        if owns and client is not None:
            await client.aclose()


def public_settings_snapshot(
    row: PasarGuardSettings | None,
    settings: Settings,
    effective: EffectivePasarGuardConfig,
) -> dict[str, Any]:
    """Non-secret view for admin UI / tests. Secrets shown as masked if set."""
    db_api_key_set = bool(row and _nonempty(row.api_key))
    env_api_key_set = bool(_nonempty(settings.pasar_guard_api_key))
    api_key_set = effective.uses_api_key
    return {
        "base_url": effective.base_url,
        "api_key_masked": "••••" if api_key_set else "—",
        "api_key_from_db": db_api_key_set,
        "auth_mode": effective.auth_mode,
        "auth_mode_label": "API Key",
        "sub_path": effective.sub_path,
        "timeout": effective.timeout,
        "source": effective.source,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "env_api_key_set": env_api_key_set,
    }


async def resolve_groups_for_plan_type(
    session: AsyncSession,
    settings: Settings,  # noqa: ARG001 - kept so call sites do not move
    plan_type: PlanType | str,  # noqa: ARG001
    *,
    store_category_id: int | None = None,
    tier: str | None = None,
) -> list[int]:
    """Panel groups for a purchase — the store group is the ONLY source.

    The group's own ``panel_group_ids`` (or its parent's, inherited), or
    ``sales_flags.premium_panel_group_ids`` when ``tier="premium"``. Nothing
    else is consulted: no product-type map, no DB default, no env default.

    Three competing sources used to exist, most-specific-first. The owner set
    the product map and nothing changed, because the category still held the
    [1, 2] the seed had copied onto it — a silent trap. One place means what
    the owner edits is what a customer gets.

    ``[]`` means the group is not for sale: the menu hides it, checkout
    refuses it and provisioning raises ``no_panel_group`` before any panel call.
    ``settings`` and ``plan_type`` stay in the signature so call sites do not move.
    """
    if store_category_id is None:
        return []
    return await _category_panel_groups(session, int(store_category_id), tier=tier)


def published_category_groups(categories: Any, cat: Any) -> list[int]:
    """Economy-tier groups of a published category, from rows already in memory.

    The same rule as ``_category_panel_groups`` (own, else the parent's) for
    menu filters that hold the whole catalog and must not query per row.
    """
    own = [int(g) for g in (getattr(cat, "panel_group_ids", None) or [])]
    if own or getattr(cat, "parent_id", None) is None:
        return own
    parent = next((c for c in categories if int(c.id) == int(cat.parent_id)), None)
    return [int(g) for g in (getattr(parent, "panel_group_ids", None) or [])]


async def _live_row(session: AsyncSession, model: Any, row_id: int) -> Any | None:
    """The row for ``row_id``, or its live twin when that row is from an old version.

    Publishing clones the catalog and mints new ids, keeping each slug. A row
    of an archived version still carries the panel groups it had back then, so
    an id held by a customer's open shelf must resolve through the slug to the
    live row, never be read as-is. A draft or published row is returned itself.
    """
    from database.models.store import StoreVersion, StoreVersionStatus

    row = await session.get(model, row_id)
    if row is None:
        return None
    version = await session.get(StoreVersion, int(row.store_version_id))
    if version is None or version.status is not StoreVersionStatus.archived:
        return row
    return await session.scalar(
        select(model)
        .join(StoreVersion, StoreVersion.id == model.store_version_id)
        .where(StoreVersion.status == StoreVersionStatus.published, model.slug == row.slug)
    )


async def live_store_category(session: AsyncSession, category_id: int) -> Any | None:
    """A store group by id, resolved to the live row by slug when the id is stale."""
    from database.models.store import StoreCategory

    return await _live_row(session, StoreCategory, int(category_id))


async def live_store_package(session: AsyncSession, package_id: int) -> Any | None:
    """A store plan by id, resolved to the live row by slug when the id is stale."""
    from database.models.store import StorePackage

    return await _live_row(session, StorePackage, int(package_id))


async def _category_panel_groups(
    session: AsyncSession, category_id: int, *, tier: str | None = None
) -> list[int]:
    """Groups for a category, inheriting from its parent when it sets none.

    ``tier="premium"`` reads ``sales_flags.premium_panel_group_ids`` first;
    empty falls through to the economy ``panel_group_ids`` column.
    """
    from database.models.store import StoreCategory

    def _premium_ids(row: StoreCategory) -> list[int]:
        flags = dict(row.sales_flags or {})
        raw = flags.get("premium_panel_group_ids") or []
        return [int(g) for g in raw] if isinstance(raw, list) else []

    cat = await live_store_category(session, category_id)
    if cat is None:
        return []
    if tier == "premium":
        premium = _premium_ids(cat)
        if premium:
            return premium
    own = [int(g) for g in (cat.panel_group_ids or [])]
    if own or cat.parent_id is None:
        return own
    parent = await live_store_category(session, int(cat.parent_id))
    if parent is None:
        return []
    if tier == "premium":
        ppremium = _premium_ids(parent)
        if ppremium:
            return ppremium
    return [int(g) for g in (parent.panel_group_ids or [])]
