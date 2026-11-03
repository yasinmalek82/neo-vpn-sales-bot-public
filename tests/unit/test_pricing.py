"""Unit tests for pricing engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.pricing import PlanType, calculate_price, load_pricing_config


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    src = Path("config/pricing.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_pricing_config(path)


def test_volume_tier_and_duration_discount(cfg) -> None:
    price = calculate_price(cfg, plan_type=PlanType.VOLUME, volume_gb=100, duration_days=90)
    # 100GB @ 10000 = 1_000_000, 10% duration discount
    assert price.original_price == 1_000_000
    assert price.discount_amount == 100_000
    assert price.final_price == 900_000


def test_volume_higher_tier_cheaper_unit(cfg) -> None:
    small = calculate_price(cfg, plan_type=PlanType.VOLUME, volume_gb=30, duration_days=30)
    large = calculate_price(cfg, plan_type=PlanType.VOLUME, volume_gb=200, duration_days=30)
    assert small.original_price == 30 * 12000
    assert large.original_price == 200 * 8000


def test_unlimited_coefficients(cfg) -> None:
    one = calculate_price(cfg, plan_type=PlanType.UNLIMITED, concurrent_users=1, duration_months=1)
    two = calculate_price(cfg, plan_type=PlanType.UNLIMITED, concurrent_users=2, duration_months=1)
    assert one.original_price == 150000
    # 150000 + 150000*0.70 = 255000
    assert two.original_price == 255000


def test_promo_code(cfg) -> None:
    price = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        promo_code="welcome10",
    )
    assert price.original_price == 50 * 12000
    assert price.discount_amount == int(price.original_price * 0.10)
    assert price.final_price == price.original_price - price.discount_amount


def test_invalid_promo(cfg) -> None:
    with pytest.raises(ValueError, match="invalid_promo_code"):
        calculate_price(
            cfg,
            plan_type=PlanType.VOLUME,
            volume_gb=30,
            duration_days=30,
            promo_code="NOPE",
        )


def test_disabled_promo_is_rejected(cfg) -> None:
    """The owner's «غیرفعال» toggle writes ``active: false``; checkout must read it.

    It did not: a switched-off code kept discounting for anyone who typed it.
    """
    cfg = dict(cfg)
    cfg["promo_codes"] = {"OFF10": {"percent": "10", "active": False}}
    with pytest.raises(ValueError, match="invalid_promo_code"):
        calculate_price(
            cfg,
            plan_type=PlanType.VOLUME,
            volume_gb=30,
            duration_days=30,
            promo_code="off10",
        )


def test_legacy_bare_percent_promo_still_applies(cfg) -> None:
    """Older seeds stored ``{"CODE": "10"}`` with no ``percent``/``active`` keys."""
    cfg = dict(cfg)
    cfg["promo_codes"] = {"OLD10": "10"}
    price = calculate_price(
        cfg, plan_type=PlanType.VOLUME, volume_gb=50, duration_days=30, promo_code="OLD10"
    )
    assert price.discount_amount == int(price.original_price * 0.10)


def test_extra_referral_discount_and_cap(cfg) -> None:
    base = calculate_price(cfg, plan_type=PlanType.VOLUME, volume_gb=50, duration_days=30)
    with_ref = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        extra_discount_percent=5,
        max_discount_percent=50,
    )
    assert with_ref.final_price < base.final_price
    assert with_ref.meta["extra_discount_percent"] == "5"

    capped = calculate_price(
        cfg,
        plan_type=PlanType.VOLUME,
        volume_gb=50,
        duration_days=30,
        promo_code="welcome10",
        extra_discount_percent=50,
        max_discount_percent=15,
    )
    assert capped.meta["discount_capped"] is True
    assert capped.meta["total_discount_percent"] == "15"
