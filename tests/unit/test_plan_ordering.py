"""Plan display ordering: auto by dimension, manual by sort_order."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.store_admin import move_draft_package
from services.store_catalog import sort_packages_for_display


def _pkg(
    *,
    id: int,
    sort_order: int,
    volume_gb: int | None = None,
    concurrent_users: int | None = None,
    title: str = "",
) -> Any:
    opts: dict[str, Any] = {}
    if volume_gb is not None:
        opts["volume_gb"] = volume_gb
    if concurrent_users is not None:
        opts["concurrent_users"] = concurrent_users
    return SimpleNamespace(
        id=id,
        sort_order=sort_order,
        options=opts,
        title_fa=title or f"p{id}",
        category_id=1,
        slug=f"p{id}",
    )


def _cat(*, kind: str = "volume", mode: str | None = None) -> Any:
    flags: dict[str, Any] = {"kind": kind}
    if mode is not None:
        flags["plan_order_mode"] = mode
    return SimpleNamespace(id=1, title_fa="گروه", sales_flags=flags, slug="g")


def test_auto_mode_sorts_volume_by_gb_regardless_of_sort_order() -> None:
    """Owner bug: 40 GB created last must still sit between 30 and 50."""
    cat = _cat(kind="volume")  # auto default
    pkgs = [
        _pkg(id=1, sort_order=10, volume_gb=50),
        _pkg(id=2, sort_order=20, volume_gb=10),
        _pkg(id=3, sort_order=30, volume_gb=100),
        _pkg(id=4, sort_order=40, volume_gb=30),
        _pkg(id=5, sort_order=50, volume_gb=15),
        _pkg(id=6, sort_order=60, volume_gb=70),
        _pkg(id=7, sort_order=70, volume_gb=20),
        _pkg(id=8, sort_order=80, volume_gb=40),  # created last
    ]
    ordered = sort_packages_for_display(pkgs, cat)
    assert [p.options["volume_gb"] for p in ordered] == [10, 15, 20, 30, 40, 50, 70, 100]


def test_auto_mode_sorts_unlimited_by_concurrent_users() -> None:
    cat = _cat(kind="unlimited")
    pkgs = [
        _pkg(id=1, sort_order=10, concurrent_users=3),
        _pkg(id=2, sort_order=20, concurrent_users=1),
        _pkg(id=3, sort_order=30, concurrent_users=2),
    ]
    ordered = sort_packages_for_display(pkgs, cat)
    assert [p.options["concurrent_users"] for p in ordered] == [1, 2, 3]


def test_auto_mode_missing_dimension_sorts_last() -> None:
    cat = _cat(kind="volume")
    pkgs = [
        _pkg(id=1, sort_order=10, volume_gb=50),
        _pkg(id=2, sort_order=5),  # no volume_gb — must be last, not first
        _pkg(id=3, sort_order=20, volume_gb=10),
    ]
    ordered = sort_packages_for_display(pkgs, cat)
    assert [p.id for p in ordered] == [3, 1, 2]


def test_manual_mode_preserves_sort_order_ignoring_gb() -> None:
    cat = _cat(kind="volume", mode="manual")
    pkgs = [
        _pkg(id=1, sort_order=30, volume_gb=10),
        _pkg(id=2, sort_order=10, volume_gb=100),
        _pkg(id=3, sort_order=20, volume_gb=50),
    ]
    ordered = sort_packages_for_display(pkgs, cat)
    assert [p.id for p in ordered] == [2, 3, 1]


@pytest.mark.asyncio
async def test_move_up_swaps_with_neighbour_and_top_is_noop() -> None:
    cat = _cat(kind="volume", mode="manual")
    p1 = _pkg(id=1, sort_order=10, volume_gb=10, title="a")
    p2 = _pkg(id=2, sort_order=20, volume_gb=20, title="b")
    p3 = _pkg(id=3, sort_order=30, volume_gb=30, title="c")
    from services import store_admin as sa

    async def fake_get_pkg(session: object, package_id: int) -> Any:
        return {1: p1, 2: p2, 3: p3}[package_id]

    async def fake_get_cat(session: object, category_id: int) -> Any:
        return cat

    async def fake_draft(session: object) -> Any:
        return SimpleNamespace(packages=[p1, p2, p3], categories=[cat])

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sa, "get_draft_package", fake_get_pkg)
    monkey.setattr(sa, "get_draft_category", fake_get_cat)
    monkey.setattr(sa, "get_or_create_draft", fake_draft)
    try:
        status, flipped, pkg, _cat_out = await move_draft_package(MagicMock(), 2, direction="up")
        assert status == "moved"
        assert flipped is False
        assert p2.sort_order == 10
        assert p1.sort_order == 20

        status2, flipped2, _, _ = await move_draft_package(MagicMock(), 2, direction="up")
        assert status2 == "at_edge"
        assert flipped2 is False
        assert p2.sort_order == 10
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_first_arrow_on_auto_freezes_every_plan_then_moves() -> None:
    cat = _cat(kind="volume")  # auto
    p_big = _pkg(id=1, sort_order=10, volume_gb=50, title="50")
    p_small = _pkg(id=2, sort_order=20, volume_gb=10, title="10")
    p_mid = _pkg(id=3, sort_order=30, volume_gb=30, title="30")
    from services import store_admin as sa

    async def fake_get_pkg(session: object, package_id: int) -> Any:
        return {1: p_big, 2: p_small, 3: p_mid}[package_id]

    async def fake_get_cat(session: object, category_id: int) -> Any:
        return cat

    async def fake_draft(session: object) -> Any:
        return SimpleNamespace(packages=[p_big, p_small, p_mid], categories=[cat])

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sa, "get_draft_package", fake_get_pkg)
    monkey.setattr(sa, "get_draft_category", fake_get_cat)
    monkey.setattr(sa, "get_or_create_draft", fake_draft)
    try:
        status, flipped, pkg, cat_out = await move_draft_package(MagicMock(), 3, direction="up")
        assert status == "moved"
        assert flipped is True
        assert cat_out.sales_flags["plan_order_mode"] == "manual"
        assert sorted(p.sort_order for p in (p_big, p_small, p_mid)) == [10, 20, 30]
        assert p_mid.sort_order == 10
        assert p_small.sort_order == 20
        assert p_big.sort_order == 30
    finally:
        monkey.undo()
