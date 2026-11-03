"""Which volume warning is due, as a pure table (no database, no panel).

«at or under» a configured percent, the LOWEST percent crossed (so a service
that jumps straight under 5 gets one message, not two), nothing for an
unlimited service, nothing once the service is not active.
"""

from __future__ import annotations

import pytest

from services.volume_warnings import HEALTHY, NO_WARNING, VolumeVerdict, volume_verdict

GB = 1024**3


def verdict(
    used_gb: float,
    *,
    limit_gb: float | None = 10,
    percents: tuple[int, ...] = (20, 5),
    status: str = "active",
) -> VolumeVerdict:
    return volume_verdict(
        percents,
        data_limit=None if limit_gb is None else int(limit_gb * GB),
        used_traffic=int(used_gb * GB),
        status=status,
    )


@pytest.mark.parametrize(
    ("used_gb", "expected_threshold"),
    [
        (7.9, None),  # 21% left: above both
        (8.0, 20),  # exactly 20% left: «at or under»
        (8.5, 20),  # 15%
        (9.49, 20),  # 5.1%
        (9.5, 5),  # exactly 5%
        (9.7, 5),  # 3%: both crossed, the lowest is sent
        (10.0, 5),  # all gone but still «active»: the panel has not flipped yet
        (12.0, 5),  # over the limit
    ],
)
def test_the_lowest_crossed_percent_is_the_one_to_warn_about(
    used_gb: float, expected_threshold: int | None
) -> None:
    got = verdict(used_gb)
    if expected_threshold is None:
        assert got.kind == HEALTHY
    else:
        assert got.kind == "warn" and got.threshold == expected_threshold


def test_jumping_straight_under_the_lowest_percent_is_one_threshold_not_two() -> None:
    assert verdict(9.8).threshold == 5


@pytest.mark.parametrize("limit", [None, 0])
def test_an_unlimited_service_never_warns_and_never_re_arms(limit: int | None) -> None:
    got = volume_verdict((20, 5), data_limit=limit, used_traffic=999 * GB, status="active")
    assert got.kind == NO_WARNING


@pytest.mark.parametrize("status", ["limited", "expired", "disabled", "on_hold", "weird"])
def test_only_an_active_service_is_warned(status: str) -> None:
    assert verdict(9.7, status=status).kind == NO_WARNING


def test_no_configured_percent_means_off() -> None:
    assert verdict(9.9, percents=()).kind == NO_WARNING


def test_a_single_percent() -> None:
    assert verdict(8.5, percents=(10,)).kind == HEALTHY
    assert verdict(9.5, percents=(10,)).threshold == 10


def test_an_active_service_above_every_percent_is_healthy_so_markers_can_re_arm() -> None:
    assert verdict(0).kind == HEALTHY
