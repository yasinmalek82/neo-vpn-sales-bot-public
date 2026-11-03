"""Pure price-matrix lookups for ready plans (no session, no IO)."""

from __future__ import annotations

from typing import Any

DEFAULT_TIER = "economy"
PREMIUM_TIER = "premium"
_ALL_TIERS = (DEFAULT_TIER, PREMIUM_TIER)


def category_durations(category: Any) -> list[int | None]:
    """Ordered durations from ``sales_flags.durations``; default ``[30]``."""
    if category is None:
        return [30]
    if isinstance(category, dict):
        flags = category.get("sales_flags") or {}
    else:
        flags = dict(getattr(category, "sales_flags", None) or {})
    if not isinstance(flags, dict):
        return [30]
    raw = flags.get("durations")
    if not isinstance(raw, list) or not raw:
        return [30]
    out: list[int | None] = []
    for item in raw:
        if item is None:
            out.append(None)
        else:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
    return out or [30]


def category_tiers(category: Any) -> list[str]:
    """``[economy]`` unless ``tiers_enabled`` is true."""
    if category is None:
        return [DEFAULT_TIER]
    if isinstance(category, dict):
        flags = category.get("sales_flags") or {}
    else:
        flags = dict(getattr(category, "sales_flags", None) or {})
    if not isinstance(flags, dict):
        return [DEFAULT_TIER]
    if bool(flags.get("tiers_enabled")):
        return [DEFAULT_TIER, PREMIUM_TIER]
    return [DEFAULT_TIER]


def _row_slug(row: Any) -> str:
    return str(getattr(row, "package_slug", "") or "")


def _row_duration(row: Any) -> int | None:
    val = getattr(row, "duration_days", None)
    if val is None:
        return None
    return int(val)


def _row_tier(row: Any) -> str:
    return str(getattr(row, "tier", "") or "")


def _row_price(row: Any) -> int:
    return int(row.price_toman)


def get_price(
    prices: list[Any],
    *,
    slug: str,
    duration_days: int | None,
    tier: str,
) -> int | None:
    """Exact cell lookup. Missing → ``None`` (not for sale). Never treat 0 as missing."""
    for row in prices:
        if _row_slug(row) != slug:
            continue
        if _row_tier(row) != tier:
            continue
        if _row_duration(row) != duration_days:
            continue
        return _row_price(row)
    return None


def available_durations(
    prices: list[Any],
    *,
    slug: str,
    tier: str,
    category: Any = None,
) -> list[int | None]:
    """Durations that have a price row, in the category's configured order."""
    configured = category_durations(category)
    have: set[int | None] = set()
    for row in prices:
        if _row_slug(row) == slug and _row_tier(row) == tier:
            have.add(_row_duration(row))
    return [d for d in configured if d in have]


def is_buyable(package: Any, prices: list[Any], *, tier: str = DEFAULT_TIER) -> bool:
    """Ready plan with at least one price row for ``tier``."""
    kind = getattr(package, "kind", None)
    kind_val = kind.value if hasattr(kind, "value") else str(kind)
    if kind_val != "ready":
        return False
    if not getattr(package, "enabled", False):
        return False
    if not getattr(package, "allow_new_sale", False):
        return False
    slug = str(getattr(package, "slug", "") or "")
    return any(_row_slug(row) == slug and _row_tier(row) == tier for row in prices)


def duration_from_package_options(options: dict[str, Any] | None) -> int | None:
    """Mirror the migration backfill: days, else months*30, else None."""
    opts = dict(options or {})
    if opts.get("duration_days") is not None:
        try:
            return int(opts["duration_days"])
        except (TypeError, ValueError):
            return None
    if opts.get("duration_months") is not None:
        try:
            return int(opts["duration_months"]) * 30
        except (TypeError, ValueError):
            return None
    return None


__all__ = [
    "DEFAULT_TIER",
    "PREMIUM_TIER",
    "category_durations",
    "category_tiers",
    "get_price",
    "available_durations",
    "is_buyable",
    "duration_from_package_options",
]
