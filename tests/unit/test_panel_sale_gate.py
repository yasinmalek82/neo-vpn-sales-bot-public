"""Unit tests for the panel sale gate evaluator (is_panel_down).

Covers all 6 state cases from plans/2026-10-10-antigravity-round2.md §M1,
and provides dedicated tests for mutations M1-MUT-1 and M1-MUT-2.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Settings
from database.base import Base
from database.models.panel_node_state import PANEL_NODE_ID, PanelNodeState
from services.panel_watch import ALERT_AFTER_CHECKS, is_panel_down

T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


def _settings(interval: int = 120) -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "SCHEDULER_ENABLED": True,
            "PANEL_WATCH_INTERVAL_SECONDS": interval,
            "PASARGUARD_API_KEY": "pg_key_test",
        }
    )


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s
    await engine.dispose()


# ── The 6-Case Unit Table ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case_name",
        "has_row",
        "status",
        "checks",
        "interval_seconds",
        "age_seconds",
        "expected_down",
    ),
    [
        # Case 1: No row in panel_node_states
        ("no_row", False, "unreachable", 2, 120, 0, False),
        # Case 2: Status connected
        ("connected", True, "connected", 5, 120, 0, False),
        # Case 3: Down with 1 check (fail open)
        ("down_1_check", True, "unreachable", 1, 120, 0, False),
        # Case 4: Down with 2 checks, fresh (gate closed)
        ("down_2_checks_fresh", True, "unreachable", 2, 120, 60, True),
        # Case 5: Down with 2 checks, stale (> 3 intervals: 361s > 360s)
        ("down_2_checks_stale", True, "unreachable", 2, 120, 361, False),
        # Exactly three intervals old is no longer «newer than three intervals»
        ("down_2_checks_359s", True, "unreachable", 2, 120, 359, True),
        ("down_2_checks_exactly_3_intervals", True, "unreachable", 2, 120, 360, False),
        # Case 6: Watcher disabled (interval 0)
        ("watcher_disabled", True, "unreachable", 2, 0, 0, False),
    ],
)
async def test_is_panel_down_unit_table(
    session: AsyncSession,
    case_name: str,
    has_row: bool,
    status: str,
    checks: int,
    interval_seconds: int,
    age_seconds: int,
    expected_down: bool,
) -> None:
    settings = _settings(interval_seconds)
    now = T0
    if has_row:
        row = PanelNodeState(
            node_id=PANEL_NODE_ID,
            name="panel",
            status=status,
            checks_in_status=checks,
            status_since=now - timedelta(seconds=age_seconds),
            last_checked_at=now - timedelta(seconds=age_seconds),
        )
        session.add(row)
        await session.commit()

    result = await is_panel_down(session, settings, now=now)
    assert (
        result is expected_down
    ), f"Case '{case_name}' expected is_panel_down={expected_down}, got {result}"


# ── Dedicated Mutation Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m1_mut_1_single_down_check_fails_open(session: AsyncSession) -> None:
    """[M1-MUT-1] One failed check must never close sales.

    Mutation check: If checks_in_status >= 2 is relaxed to >= 1, this test fails.
    """
    settings = _settings(120)
    now = T0
    row = PanelNodeState(
        node_id=PANEL_NODE_ID,
        name="panel",
        status="unreachable",
        checks_in_status=1,  # exactly 1 check
        status_since=now,
        last_checked_at=now,
    )
    session.add(row)
    await session.commit()

    assert await is_panel_down(session, settings, now=now) is False, (
        "Single check down must fail open (return False). "
        "Failing this indicates M1-MUT-1 violation (checks_in_status >= 1 instead of >= 2)."
    )


@pytest.mark.asyncio
async def test_m1_mut_2_stale_row_fails_open(session: AsyncSession) -> None:
    """[M1-MUT-2] A down row older than 3 intervals must never close sales.

    Mutation check: If staleness check is removed or multiplier changed, this fails.
    Threshold: 3 * 120s = 360s. At 361s it must fail open (return False).
    """
    settings = _settings(120)
    now = T0
    row = PanelNodeState(
        node_id=PANEL_NODE_ID,
        name="panel",
        status="unreachable",
        checks_in_status=ALERT_AFTER_CHECKS,  # 2 checks
        status_since=now - timedelta(seconds=400),
        last_checked_at=now - timedelta(seconds=361),  # Stale (> 360s)
    )
    session.add(row)
    await session.commit()

    assert await is_panel_down(session, settings, now=now) is False, (
        "Stale panel row must fail open (return False). "
        "Failing this indicates M1-MUT-2 violation (staleness check missing/infinite)."
    )


# ── Edge Case & Freshness Boundaries ───────────────────────────────────────


@pytest.mark.asyncio
async def test_is_panel_down_freshness_boundary(session: AsyncSession) -> None:
    """Boundary check: 359s is fresh (True), 361s is stale (False)."""
    settings = _settings(120)
    now = T0
    row = PanelNodeState(
        node_id=PANEL_NODE_ID,
        name="panel",
        status="error",
        checks_in_status=2,
        status_since=now - timedelta(seconds=500),
        last_checked_at=now - timedelta(seconds=359),  # Just inside 3 * 120s
    )
    session.add(row)
    await session.commit()

    # Fresh -> True
    assert await is_panel_down(session, settings, now=now) is True

    # Age past boundary -> False
    row.last_checked_at = now - timedelta(seconds=361)
    await session.commit()
    assert await is_panel_down(session, settings, now=now) is False


@pytest.mark.asyncio
async def test_is_panel_down_supports_naive_sqlite_datetimes(session: AsyncSession) -> None:
    """SQLite returns naive datetimes; must not crash on timezone comparison."""
    settings = _settings(120)
    now_aware = T0
    now_naive = T0.replace(tzinfo=None)

    row = PanelNodeState(
        node_id=PANEL_NODE_ID,
        name="panel",
        status="unreachable",
        checks_in_status=2,
        status_since=now_naive,
        last_checked_at=now_naive,
    )
    session.add(row)
    await session.commit()

    # Works with both aware and naive now arguments
    assert await is_panel_down(session, settings, now=now_aware) is True
    assert await is_panel_down(session, settings, now=now_naive) is True
