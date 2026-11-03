"""Regression coverage for fixed ready-package price arithmetic."""

from __future__ import annotations

import pytest

from services.checkout import price_from_fixed as _price_from_fixed
from services.pricing import calculate_fixed_price


def test_fixed_price_uses_half_up_for_a_half_toman_discount() -> None:
    """The old float and banker's-rounding expression loses this half toman."""
    price = _price_from_fixed(
        {"fixed_price_toman": 100_005},
        promo_code=None,
        extra_discount_percent=10,
        max_discount_percent=None,
    )

    assert price.original_price == 100_005
    assert price.discount_amount == 10_001
    assert price.final_price == 90_004


@pytest.mark.parametrize(
    ("price_toman", "extra_discount_percent", "expected_discount", "expected_final"),
    [
        (100_000, 10, 10_000, 90_000),
        (100_005, 10, 10_001, 90_004),
        (9_007_199_254_740_997, 10, 900_719_925_474_100, 8_106_479_329_266_897),
    ],
)
def test_fixed_price_calculation_keeps_integer_precision(
    price_toman: int,
    extra_discount_percent: int,
    expected_discount: int,
    expected_final: int,
) -> None:
    """Float conversion would change the large total or lose a half toman."""
    price = calculate_fixed_price(
        price_toman=price_toman,
        extra_discount_percent=extra_discount_percent,
        max_discount_percent=None,
        forbid_extra_discount=False,
    )

    assert price.discount_amount == expected_discount
    assert price.final_price == expected_final


def test_fixed_price_zero_extra_discount_leaves_the_price_unchanged() -> None:
    price = calculate_fixed_price(
        price_toman=100_005,
        extra_discount_percent=0,
        max_discount_percent=50,
        forbid_extra_discount=False,
    )

    assert price.discount_amount == 0
    assert price.final_price == 100_005


def test_fixed_price_applies_the_configured_discount_cap() -> None:
    price = calculate_fixed_price(
        price_toman=100_005,
        extra_discount_percent=20,
        max_discount_percent=10,
        forbid_extra_discount=False,
    )

    assert price.discount_amount == 10_001
    assert price.final_price == 90_004


def test_fixed_price_forbids_an_extra_discount_but_retains_its_source_metadata() -> None:
    price = calculate_fixed_price(
        price_toman=100_005,
        extra_discount_percent=10,
        max_discount_percent=None,
        forbid_extra_discount=True,
    )

    assert price.discount_amount == 0
    assert price.final_price == 100_005
    assert price.meta["extra_discount_percent"] == 10


def test_fixed_price_forbids_an_extra_discount_even_with_a_negative_legacy_cap() -> None:
    """A cap must not turn a forbidden discount into a surcharge."""
    price = calculate_fixed_price(
        price_toman=100_005,
        extra_discount_percent=10,
        max_discount_percent=-1,
        forbid_extra_discount=True,
    )

    assert price.discount_amount == 0
    assert price.final_price == 100_005


def test_fixed_price_copies_metadata_and_currency() -> None:
    metadata = {"pricing_mode": "fixed_ready", "volume_gb": 50}
    price = calculate_fixed_price(
        price_toman=100_005,
        extra_discount_percent=10,
        max_discount_percent=None,
        forbid_extra_discount=False,
        currency="IRR",
        metadata=metadata,
    )

    assert price.currency == "IRR"
    assert price.meta == {
        "pricing_mode": "fixed_ready",
        "volume_gb": 50,
        "extra_discount_percent": 10,
    }
    assert price.meta is not metadata


def test_fixed_price_adapter_preserves_promo_marker_and_dimensions() -> None:
    price = _price_from_fixed(
        {
            "fixed_price_toman": 100_005,
            "store_package_id": 7,
            "volume_gb": 50,
            "duration_days": 30,
        },
        promo_code="SAVE10",
        extra_discount_percent=10,
        max_discount_percent=10,
        currency="IRR",
    )

    assert price.currency == "IRR"
    assert price.meta["pricing_mode"] == "fixed_ready"
    assert price.meta["store_package_id"] == 7
    assert price.meta["volume_gb"] == 50
    assert price.meta["duration_days"] == 30
    assert price.meta["promo_code"] == "SAVE10"
