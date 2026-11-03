"""Independent pricing engine (no Telegram / PasarGuard dependency).

All coefficients and tiers come from a pricing config dict — not hardcoded business rules.
Monetary math uses Decimal; public API returns integer toman amounts.

Config source (STORE_CORE):
- Prefer ``services.store_config.load_pricing_config_async(session)`` which returns the
  published StoreVersion.pricing_document once seeded.
- ``load_pricing_config(path)`` remains for bootstrap/tests **before** any published
  store row exists. After publish, purchase must not silently fall back to the file.

Optional ``cfg["sales_rules"]["discount_combine"]``:
- ``"sum"`` (legacy default when key absent): duration + promo + extra
- ``"best_of_promo_vs_extra"`` (seeded store default): duration + max(promo, extra)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class PlanType(str, Enum):
    VOLUME = "volume"
    UNLIMITED = "unlimited"


@dataclass(frozen=True)
class PriceResult:
    original_price: int
    discount_amount: int
    final_price: int
    currency: str
    meta: dict[str, Any]


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_fixed_price(
    *,
    price_toman: int,
    extra_discount_percent: int,
    max_discount_percent: int | None,
    forbid_extra_discount: bool,
    currency: str = "IRT",
    metadata: dict[str, Any] | None = None,
) -> PriceResult:
    """Price a fixed ready package with integer-toman HALF_UP arithmetic."""
    original = int(price_toman)
    percent = 0
    if not forbid_extra_discount:
        percent = max(0, int(extra_discount_percent or 0))
    if not forbid_extra_discount and max_discount_percent is not None:
        percent = min(percent, int(max_discount_percent))
    discount = _money(Decimal(original) * Decimal(percent) * Decimal("0.01"))
    final = max(0, original - discount)
    result_metadata = dict(metadata or {})
    result_metadata["extra_discount_percent"] = int(extra_discount_percent or 0)
    return PriceResult(
        original_price=original,
        discount_amount=discount,
        final_price=final,
        currency=currency,
        meta=result_metadata,
    )


def calculate_fixed_price_with_promo(
    cfg: dict[str, Any],
    *,
    price_toman: int,
    promo_code: str,
    extra_discount_percent: int,
    max_discount_percent: int | None,
    currency: str = "IRT",
    metadata: dict[str, Any] | None = None,
) -> PriceResult:
    """A promo code on a ready plan discounts **the plan's own price**.

    Without this the checkout fell through to ``calculate_price`` — the
    custom-volume rate engine — and a 255,000 plan was re-priced at
    50 GB × 12,000 = 600,000 before the percent came off. The duration discount
    is zero here: the owner already chose the fixed price for that duration.
    """
    original = Decimal(int(price_toman))
    discount, final, meta = _apply_promo(
        cfg,
        original=original,
        duration_discount_percent=Decimal("0"),
        promo_code=promo_code,
        extra_discount_percent=_d(extra_discount_percent or 0),
        max_discount_percent=None if max_discount_percent is None else _d(max_discount_percent),
    )
    result_metadata = dict(metadata or {})
    result_metadata.update(meta)
    return PriceResult(
        original_price=int(price_toman),
        discount_amount=_money(discount),
        final_price=_money(final),
        currency=currency,
        meta=result_metadata,
    )


def load_pricing_config(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pricing config must be a JSON object")
    return data


def _volume_unit_price(cfg: dict[str, Any], volume_gb: int) -> Decimal:
    tiers = cfg["volume_plan"]["tiers"]
    for tier in tiers:
        max_gb = tier.get("max_gb")
        if max_gb is None or volume_gb <= int(max_gb):
            return _d(tier["price_per_gb_toman"])
    return _d(tiers[-1]["price_per_gb_toman"])


def _duration_discount_percent_volume(cfg: dict[str, Any], duration_days: int) -> Decimal:
    best = Decimal("0")
    for row in cfg["volume_plan"].get("duration_discounts", []):
        if duration_days >= int(row["min_days"]):
            best = max(best, _d(row["percent"]))
    return best


def _duration_discount_percent_unlimited(cfg: dict[str, Any], duration_months: int) -> Decimal:
    best = Decimal("0")
    for row in cfg["unlimited_plan"].get("duration_discounts", []):
        if duration_months >= int(row["min_months"]):
            best = max(best, _d(row["percent"]))
    return best


def _unlimited_users_price(cfg: dict[str, Any], concurrent_users: int) -> Decimal:
    if concurrent_users < 1:
        raise ValueError("concurrent_users must be >= 1")
    base = _d(cfg["unlimited_plan"]["base_price_first_user_toman"])
    coeffs = [_d(c) for c in cfg["unlimited_plan"]["additional_user_coefficients"]]
    total = base
    for i in range(2, concurrent_users + 1):
        idx = min(i - 2, len(coeffs) - 1)
        total += base * coeffs[idx]
    return total


def _apply_promo(
    cfg: dict[str, Any],
    *,
    original: Decimal,
    duration_discount_percent: Decimal,
    promo_code: str | None,
    extra_discount_percent: Decimal = Decimal("0"),
    max_discount_percent: Decimal | None = None,
) -> tuple[Decimal, Decimal, dict[str, Any]]:
    promo_percent = Decimal("0")
    promo_applied: str | None = None
    if promo_code:
        code = promo_code.strip().upper()
        promo = (cfg.get("promo_codes") or {}).get(code)
        if promo is None:
            raise ValueError("invalid_promo_code")
        # Two shapes coexist: the seed's bare percent («"10"») and the store
        # admin's ``{"percent", "active"}`` dict. The owner's «غیرفعال» toggle
        # only writes ``active: false`` — it is refused here or nowhere.
        if isinstance(promo, dict):
            if not bool(promo.get("active", True)):
                raise ValueError("invalid_promo_code")
            promo_percent = _d(promo["percent"])
        else:
            promo_percent = _d(promo)
        promo_applied = code

    if extra_discount_percent < 0:
        raise ValueError("extra_discount_percent must be >= 0")

    rules = cfg.get("sales_rules") if isinstance(cfg.get("sales_rules"), dict) else {}
    combine = str((rules or {}).get("discount_combine") or "sum")
    if combine == "best_of_promo_vs_extra":
        variable_percent = max(promo_percent, extra_discount_percent)
        total_percent = duration_discount_percent + variable_percent
    else:
        # Legacy / explicit "sum": stack duration + promo + extra.
        total_percent = duration_discount_percent + promo_percent + extra_discount_percent
    capped = False
    if max_discount_percent is not None and total_percent > max_discount_percent:
        total_percent = max_discount_percent
        capped = True
    discount = (original * total_percent / Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    final = original - discount
    if final < 0:
        final = Decimal("0")
        discount = original
    return (
        discount,
        final,
        {
            "duration_discount_percent": str(duration_discount_percent),
            "promo_code": promo_applied,
            "promo_percent": str(promo_percent),
            "extra_discount_percent": str(extra_discount_percent),
            "discount_combine": combine,
            "total_discount_percent": str(total_percent),
            "discount_capped": capped,
        },
    )


def calculate_price(
    cfg: dict[str, Any],
    *,
    plan_type: PlanType | str,
    volume_gb: int | None = None,
    duration_days: int | None = None,
    concurrent_users: int | None = None,
    duration_months: int | None = None,
    promo_code: str | None = None,
    extra_discount_percent: int | float | Decimal = 0,
    max_discount_percent: int | float | Decimal | None = None,
) -> PriceResult:
    """Calculate original, discount, and final price in toman (ints).

    ``extra_discount_percent`` is referral/extra discount. Combine mode comes from
    ``cfg["sales_rules"]["discount_combine"]`` (default ``sum`` for bare JSON;
    seeded store uses ``best_of_promo_vs_extra``). Optional ``max_discount_percent``
    caps the total percent after combine.
    """
    plan_type = PlanType(plan_type)
    currency = str(cfg.get("currency", "IRT"))
    extra = _d(extra_discount_percent)
    max_pct = None if max_discount_percent is None else _d(max_discount_percent)

    if plan_type is PlanType.VOLUME:
        if volume_gb is None or duration_days is None:
            raise ValueError("volume plan requires volume_gb and duration_days")
        if volume_gb <= 0 or duration_days <= 0:
            raise ValueError("volume_gb and duration_days must be > 0")
        unit = _volume_unit_price(cfg, volume_gb)
        original = unit * Decimal(volume_gb)
        dur_pct = _duration_discount_percent_volume(cfg, duration_days)
        discount, final, meta = _apply_promo(
            cfg,
            original=original,
            duration_discount_percent=dur_pct,
            promo_code=promo_code,
            extra_discount_percent=extra,
            max_discount_percent=max_pct,
        )
        meta.update(
            {
                "plan_type": plan_type.value,
                "volume_gb": volume_gb,
                "duration_days": duration_days,
                "unit_price_per_gb": str(unit),
            }
        )
        return PriceResult(
            original_price=_money(original),
            discount_amount=_money(discount),
            final_price=_money(final),
            currency=currency,
            meta=meta,
        )

    if concurrent_users is None or duration_months is None:
        raise ValueError("unlimited plan requires concurrent_users and duration_months")
    if concurrent_users <= 0 or duration_months <= 0:
        raise ValueError("concurrent_users and duration_months must be > 0")
    monthly = _unlimited_users_price(cfg, concurrent_users)
    original = monthly * Decimal(duration_months)
    dur_pct = _duration_discount_percent_unlimited(cfg, duration_months)
    discount, final, meta = _apply_promo(
        cfg,
        original=original,
        duration_discount_percent=dur_pct,
        promo_code=promo_code,
        extra_discount_percent=extra,
        max_discount_percent=max_pct,
    )
    meta.update(
        {
            "plan_type": plan_type.value,
            "concurrent_users": concurrent_users,
            "duration_months": duration_months,
            "monthly_price": str(monthly),
        }
    )
    return PriceResult(
        original_price=_money(original),
        discount_amount=_money(discount),
        final_price=_money(final),
        currency=currency,
        meta=meta,
    )
