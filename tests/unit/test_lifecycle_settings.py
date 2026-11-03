"""The owner's numbers for warnings and for deleting ended services.

One settings row, read by the jobs. These tests pin what the screen promises:
a typed number is checked where it is typed and the range is stated, a missing
row is not a write, and «روشن» cannot be reached without a fresh rehearsal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401 - register mappers
from database.base import Base
from database.models.lifecycle import (
    DELETE_MODE_OFF,
    DELETE_MODE_ON,
    DELETE_MODE_REHEARSAL,
    LifecycleSettings,
)
from services.lifecycle_settings import (
    REHEARSAL_VALID_DAYS,
    LifecycleConfig,
    SettingsError,
    load_config,
    parse_day_list,
    parse_days_number,
    parse_percent_list,
    save_number,
    set_delete_mode,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


# --- typed values ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 1", [3, 1]),
        ("۳ ۱", [3, 1]),
        ("1,3", [3, 1]),
        ("3،1", [3, 1]),
        ("3 3 1", [3, 1]),
        ("0", []),
        ("۰", []),
        ("", []),
        ("   ", []),
        ("365", [365]),
    ],
)
def test_a_day_list_is_read_in_either_digits_and_zero_or_empty_turns_it_off(
    raw: str, expected: list[int]
) -> None:
    assert parse_day_list(raw) == expected


@pytest.mark.parametrize("raw", ["366", "-1", "abc", "1.5", "3 x", "0 3", "1000"])
def test_a_bad_day_list_is_refused_with_the_range_in_the_message(raw: str) -> None:
    with pytest.raises(SettingsError) as caught:
        parse_day_list(raw)
    assert "۳۶۵" in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20 5", [20, 5]), ("5 20", [20, 5]), ("۵", [5]), ("0", []), ("", []), ("99", [99])],
)
def test_a_percent_list(raw: str, expected: list[int]) -> None:
    assert parse_percent_list(raw) == expected


@pytest.mark.parametrize("raw", ["100", "0 5", "-5", "x", "20.5"])
def test_a_bad_percent_list_names_the_range(raw: str) -> None:
    with pytest.raises(SettingsError) as caught:
        parse_percent_list(raw)
    assert "۹۹" in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"), [("10", 10), ("۱۰", 10), ("0", 0), ("365", 365), (" 7 ", 7)]
)
def test_a_days_number(raw: str, expected: int) -> None:
    assert parse_days_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "366", "-1", "ده", "1.5", "1 2"])
def test_a_bad_days_number_names_the_range(raw: str) -> None:
    with pytest.raises(SettingsError) as caught:
        parse_days_number(raw)
    assert "۳۶۵" in str(caught.value)


# --- the row ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_row_the_defaults_come_from_the_env_and_nothing_is_written(
    session: AsyncSession,
) -> None:
    config = await load_config(session, seed_days=[7, 2])
    assert config == LifecycleConfig(
        warn_days=(7, 2),
        warn_volume_percents=(20, 5),
        delete_mode=DELETE_MODE_OFF,
        delete_after_expiry_days=10,
        delete_after_limited_days=10,
        delete_trial_after_days=1,
        delete_warn_days_before=3,
        last_rehearsal_at=None,
    )
    assert await session.scalar(select(func.count()).select_from(LifecycleSettings)) == 0


@pytest.mark.asyncio
async def test_saving_one_number_keeps_the_env_seed_for_the_others(
    session: AsyncSession,
) -> None:
    await save_number(session, seed_days=[3, 1], field="delete_after_expiry_days", value=20)
    await session.commit()
    config = await load_config(session, seed_days=[9, 9])  # the env changed: the row wins
    assert config.delete_after_expiry_days == 20
    assert config.warn_days == (3, 1)


@pytest.mark.asyncio
async def test_a_saved_list_replaces_the_env_seed_and_empty_means_off(
    session: AsyncSession,
) -> None:
    await save_number(session, seed_days=[3, 1], field="warn_days", value=[])
    await session.commit()
    assert (await load_config(session, seed_days=[3, 1])).warn_days == ()


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_not_stored(session: AsyncSession) -> None:
    with pytest.raises(SettingsError):
        await save_number(session, seed_days=[3], field="delete_mode", value=1)


# --- «روشن» needs a fresh rehearsal -----------------------------------------


@pytest.mark.asyncio
async def test_on_is_refused_without_a_rehearsal_and_says_why(session: AsyncSession) -> None:
    with pytest.raises(SettingsError) as caught:
        await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON)
    assert "تمرین" in str(caught.value)
    assert (await load_config(session, seed_days=[3])).delete_mode == DELETE_MODE_OFF


@pytest.mark.asyncio
async def test_on_is_allowed_after_a_rehearsal_within_seven_days(session: AsyncSession) -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL, now=now)
    row = await session.get(LifecycleSettings, 1)
    assert row is not None
    row.last_rehearsal_at = now - timedelta(days=REHEARSAL_VALID_DAYS)
    await session.commit()
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)
    assert (await load_config(session, seed_days=[3])).delete_mode == DELETE_MODE_ON


@pytest.mark.asyncio
async def test_a_rehearsal_older_than_seven_days_does_not_count(session: AsyncSession) -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL, now=now)
    row = await session.get(LifecycleSettings, 1)
    assert row is not None
    row.last_rehearsal_at = now - timedelta(days=REHEARSAL_VALID_DAYS, seconds=1)
    await session.commit()
    with pytest.raises(SettingsError):
        await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)


@pytest.mark.asyncio
async def test_changing_a_delete_number_voids_the_rehearsal(session: AsyncSession) -> None:
    """The rehearsal listed what the OLD numbers would delete."""
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL, now=now)
    row = await session.get(LifecycleSettings, 1)
    assert row is not None
    row.last_rehearsal_at = now
    await session.commit()
    await save_number(session, seed_days=[3], field="delete_after_expiry_days", value=2)
    await session.commit()
    with pytest.raises(SettingsError):
        await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)


@pytest.mark.asyncio
async def test_a_warning_number_does_not_void_the_rehearsal(session: AsyncSession) -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL, now=now)
    row = await session.get(LifecycleSettings, 1)
    assert row is not None
    row.last_rehearsal_at = now
    await session.commit()
    await save_number(session, seed_days=[3], field="warn_days", value=[5])
    await session.commit()
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)


@pytest.mark.asyncio
async def test_off_and_rehearsal_are_always_allowed(session: AsyncSession) -> None:
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_OFF)
    assert (await load_config(session, seed_days=[3])).delete_mode == DELETE_MODE_OFF


# --- the adversarial review of 66ff2aa ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_changing_a_deletion_number_while_deleting_is_on_goes_back_to_rehearsal(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL, now=now)
    row = await session.get(LifecycleSettings, 1)
    assert row is not None
    row.last_rehearsal_at = now
    await session.commit()
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)
    await save_number(session, seed_days=[3], field="delete_after_expiry_days", value=2)
    await session.commit()
    config = await load_config(session, seed_days=[3])
    assert config.delete_mode == DELETE_MODE_REHEARSAL and config.last_rehearsal_at is None
    # A warning number is not a deletion number: deleting stays on.
    await session.refresh(row)
    row.last_rehearsal_at = now
    await session.commit()
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_ON, now=now)
    await save_number(session, seed_days=[3], field="warn_days", value=[5])
    await session.commit()
    assert (await load_config(session, seed_days=[3])).delete_mode == DELETE_MODE_ON


@pytest.mark.asyncio
async def test_b1c_turning_deleting_on_from_off_clears_every_limited_clock(
    session: AsyncSession,
) -> None:
    from database.models import PasarGuardAccount, User

    user = User(telegram_id=7, first_name="A", referral_code="LIM00001", language_code="fa")
    session.add(user)
    await session.flush()
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=1,
            panel_username="lim_x",
            limited_since=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    await session.commit()
    await set_delete_mode(session, seed_days=[3], mode=DELETE_MODE_REHEARSAL)
    await session.commit()
    account = (await session.execute(select(PasarGuardAccount))).scalar_one()
    await session.refresh(account)
    assert account.limited_since is None
