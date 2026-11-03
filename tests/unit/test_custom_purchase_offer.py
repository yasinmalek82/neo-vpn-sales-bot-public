"""«Who sees custom»: the group's three states, the one function, the typed volume's limits."""

from __future__ import annotations

import pytest

from services.store_catalog import (
    CUSTOM_VOLUME_GB_MAX,
    CUSTOM_VOLUME_GB_MIN,
    custom_purchase_available,
    custom_purchase_mode,
    custom_purchase_offered,
    next_custom_purchase_mode,
    normalize_category_sales_flags,
    parse_custom_volume_gb,
)

VOLUME_PLAN = {
    "volume_plan": {"volume_options_gb": [30], "duration_options_days": [30]},
    "unlimited_plan": {"user_options": [1], "duration_options_months": [1]},
}


def _cat(**flags: object) -> dict:
    return {"sales_flags": {"kind": "volume", **flags}}


# --------------------------------------------------------------------------- #
# Stored shape and old values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("flags", "mode"),
    [
        ({}, "everyone"),  # the old default was on
        ({"allow_custom_purchase": True}, "everyone"),
        ({"allow_custom_purchase": False}, "off"),
        ({"custom_purchase_mode": "off"}, "off"),
        ({"custom_purchase_mode": "resellers"}, "resellers"),
        ({"custom_purchase_mode": "everyone"}, "everyone"),
        # The new key is the master; the old bool is only the fallback.
        ({"custom_purchase_mode": "resellers", "allow_custom_purchase": True}, "resellers"),
        ({"custom_purchase_mode": "nonsense", "allow_custom_purchase": False}, "off"),
    ],
)
def test_old_and_new_stored_values_read_as_one_of_three_states(flags: dict, mode: str) -> None:
    assert custom_purchase_mode(_cat(**flags)) == mode


def test_no_group_reads_as_everyone() -> None:
    assert custom_purchase_mode(None) == "everyone"


def test_the_state_cycles_off_resellers_everyone() -> None:
    assert [next_custom_purchase_mode(m) for m in ("off", "resellers", "everyone")] == [
        "resellers",
        "everyone",
        "off",
    ]


@pytest.mark.parametrize("mode", ["off", "resellers", "everyone"])
def test_normalize_writes_the_state_and_keeps_the_old_flag_in_step(mode: str) -> None:
    out = normalize_category_sales_flags({}, kind="volume", custom_purchase_mode=mode)
    assert out["custom_purchase_mode"] == mode
    # An older reader never sees custom for retail customers that the new one withholds.
    assert out["allow_custom_purchase"] is (mode == "everyone")
    assert custom_purchase_mode({"sales_flags": out}) == mode


def test_normalize_of_an_old_store_keeps_what_it_meant() -> None:
    off = normalize_category_sales_flags({"allow_custom_purchase": False}, kind="volume")
    on = normalize_category_sales_flags({"allow_custom_purchase": True}, kind="volume")
    fresh = normalize_category_sales_flags({}, kind="volume")
    assert [custom_purchase_mode({"sales_flags": f}) for f in (off, on, fresh)] == [
        "off",
        "everyone",
        "everyone",
    ]


def test_an_unknown_state_is_refused_on_write() -> None:
    with pytest.raises(ValueError):
        normalize_category_sales_flags({}, kind="volume", custom_purchase_mode="sometimes")


# --------------------------------------------------------------------------- #
# The one function
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mode", "is_reseller", "has_rate", "offered"),
    [
        ("off", False, False, False),
        ("off", True, True, False),
        ("resellers", False, False, False),
        ("resellers", True, False, False),
        ("resellers", True, True, True),
        ("everyone", False, False, True),
        ("everyone", True, False, False),  # no silent retail price for a shop
        ("everyone", True, True, True),
    ],
)
def test_who_is_offered_custom(mode: str, is_reseller: bool, has_rate: bool, offered: bool) -> None:
    cat = _cat(custom_purchase_mode=mode)
    assert (
        custom_purchase_offered(cat, VOLUME_PLAN, is_reseller=is_reseller, has_per_gb_rate=has_rate)
        is offered
    )


def test_a_rate_alone_does_not_make_a_retail_customer_a_reseller() -> None:
    cat = _cat(custom_purchase_mode="resellers")
    assert not custom_purchase_offered(cat, VOLUME_PLAN, is_reseller=False, has_per_gb_rate=True)


def test_nothing_is_offered_when_the_builder_cannot_be_opened() -> None:
    cat = _cat(custom_purchase_mode="everyone")
    assert not custom_purchase_available(cat, {})
    assert not custom_purchase_offered(cat, {}, is_reseller=False, has_per_gb_rate=False)
    assert custom_purchase_available(cat, VOLUME_PLAN)


@pytest.mark.parametrize("mode", ["off", "resellers", "everyone"])
@pytest.mark.parametrize(("is_reseller", "has_rate"), [(False, False), (True, False), (True, True)])
def test_an_unlimited_group_never_offers_custom_whatever_it_is_set_to(
    mode: str, is_reseller: bool, has_rate: bool
) -> None:
    """Unlimited is sold by users and months; a volume rate has nothing to price there."""
    cat = {"sales_flags": {"kind": "unlimited", "custom_purchase_mode": mode}}
    assert not custom_purchase_offered(
        cat, VOLUME_PLAN, is_reseller=is_reseller, has_per_gb_rate=has_rate
    )


def test_the_group_less_unlimited_stand_in_offers_nothing_either() -> None:
    cat = {"sales_flags": {"kind": "unlimited"}}
    assert not custom_purchase_offered(cat, VOLUME_PLAN, is_reseller=True, has_per_gb_rate=True)


# --------------------------------------------------------------------------- #
# The typed volume
# --------------------------------------------------------------------------- #


def test_the_limits_are_one_to_a_thousand() -> None:
    assert (CUSTOM_VOLUME_GB_MIN, CUSTOM_VOLUME_GB_MAX) == (1, 1000)


@pytest.mark.parametrize(
    ("raw", "gb"),
    [("37", 37), ("۳۷", 37), ("٣٧", 37), (" 37 ", 37), ("1", 1), ("1000", 1000), ("۱٬۰۰۰", 1000)],
)
def test_a_whole_number_in_range_is_read(raw: str, gb: int) -> None:
    assert parse_custom_volume_gb(raw) == gb


@pytest.mark.parametrize(
    "raw", ["0", "-5", "12.5", "abc", "", "37 GB", "1001", "99999", "²", "۰", "٫", "1e3"]
)
def test_anything_else_is_not_a_volume(raw: str) -> None:
    assert parse_custom_volume_gb(raw) is None
