"""Manual service operations against a faked panel.

These write to a live VPN account, so the properties that matter are: a retry
after a timeout must not double-apply, and an unlimited plan must not be
quietly turned into a metered one.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from api_client.exceptions import PasarGuardError
from database.base import Base
from database.models import User
from database.models.pasarguard_account import PasarGuardAccount
from services.service_ops import (
    MAX_MANUAL_DAYS,
    ServiceOpError,
    add_data,
    extend_days,
    get_snapshot,
    set_enabled,
)

GB = 1024**3
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class FakePanel:
    """Minimal stand-in that records what was written."""

    def __init__(
        self,
        *,
        expire: int | None,
        data_limit: int | None,
        used: int = 0,
        status: str = "active",
    ) -> None:
        self.expire = expire
        self.data_limit = data_limit
        self.used = used
        self.status = status
        self.modify_calls: list[dict] = []
        self.disable_calls: list[bool] = []
        self.fail_next = False

    async def get_user(self, username: str):  # noqa: ANN201
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value=self.status),
            expire=self.expire,
            data_limit=self.data_limit,
            used_traffic=self.used,
        )

    async def extend_subscription(
        self, username: str, *, expire, data_limit=None
    ):  # noqa: ANN001, ANN201
        if self.fail_next:
            self.fail_next = False
            raise PasarGuardError("boom")
        # Recorded so a test can assert extend never rewrites the data cap.
        self.modify_calls.append({"expire": expire, "data_limit": data_limit})
        self.expire = expire
        return await self.get_user(username)

    async def increase_data_limit(self, username: str, data_limit: int):  # noqa: ANN201
        self.modify_calls.append({"data_limit": data_limit})
        self.data_limit = data_limit
        return await self.get_user(username)

    async def disable_user(self, username: str, *, disabled: bool = True):  # noqa: ANN201
        self.disable_calls.append(disabled)
        self.status = "disabled" if disabled else "active"
        return await self.get_user(username)


async def _account(session: AsyncSession) -> PasarGuardAccount:
    user = User(telegram_id=1, referral_code="R1")
    session.add(user)
    await session.flush()
    account = PasarGuardAccount(user_id=user.id, panel_username="yasin_ab12", panel_user_id=7)
    session.add(account)
    await session.flush()
    await session.commit()
    return account


# --------------------------------------------------------------------------- #
# Extend
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extend_pushes_the_expiry_out(session: AsyncSession) -> None:
    account = await _account(session)
    expiry = int((NOW + timedelta(days=10)).timestamp())
    panel = FakePanel(expire=expiry, data_limit=50 * GB)

    snap = await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]
    assert snap.expire_unix == expiry + 7 * 86_400


@pytest.mark.asyncio
async def test_extending_twice_is_not_double_applied_from_one_read(
    session: AsyncSession,
) -> None:
    """Each call re-reads the panel and writes an absolute date, so a retry
    after a timeout lands on the same day rather than fourteen days out."""
    account = await _account(session)
    expiry = int((NOW + timedelta(days=10)).timestamp())
    panel = FakePanel(expire=expiry, data_limit=50 * GB)

    first = await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]
    # A second deliberate extension moves on from the new date, not the old one.
    second = await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]
    assert second.expire_unix == first.expire_unix + 7 * 86_400
    # Extending must never touch the data cap.
    assert all(call["data_limit"] is None for call in panel.modify_calls)


@pytest.mark.asyncio
async def test_extending_an_expired_service_gives_a_full_window(
    session: AsyncSession,
) -> None:
    """Otherwise the customer pays for days that are already in the past."""
    account = await _account(session)
    panel = FakePanel(expire=int((NOW - timedelta(days=30)).timestamp()), data_limit=50 * GB)

    snap = await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]
    assert snap.expire_unix == int(NOW.timestamp()) + 7 * 86_400


@pytest.mark.asyncio
async def test_extend_rejects_absurd_and_negative_values(
    session: AsyncSession,
) -> None:
    account = await _account(session)
    panel = FakePanel(expire=int(NOW.timestamp()), data_limit=50 * GB)
    for days in (0, -5, MAX_MANUAL_DAYS + 1):
        with pytest.raises(ServiceOpError):
            await extend_days(session, panel, account_id=account.id, days=days, now=NOW)  # type: ignore[arg-type]
    assert panel.modify_calls == []


@pytest.mark.asyncio
async def test_a_panel_failure_surfaces_in_persian(session: AsyncSession) -> None:
    account = await _account(session)
    panel = FakePanel(expire=int(NOW.timestamp()), data_limit=50 * GB)
    panel.fail_next = True
    with pytest.raises(ServiceOpError) as exc:
        await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]
    assert "تمدید انجام نشد" in str(exc.value)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_data_raises_the_cap(session: AsyncSession) -> None:
    account = await _account(session)
    panel = FakePanel(expire=None, data_limit=50 * GB, used=10 * GB)

    snap = await add_data(session, panel, account_id=account.id, gigabytes=20)  # type: ignore[arg-type]
    assert snap.data_limit_bytes == 70 * GB
    assert snap.remaining_bytes == 60 * GB


@pytest.mark.asyncio
async def test_adding_data_to_an_unlimited_service_is_refused(
    session: AsyncSession,
) -> None:
    """Writing a cap would quietly convert an unlimited plan into a metered one."""
    account = await _account(session)
    panel = FakePanel(expire=None, data_limit=0)

    with pytest.raises(ServiceOpError) as exc:
        await add_data(session, panel, account_id=account.id, gigabytes=20)  # type: ignore[arg-type]
    assert "نامحدود" in str(exc.value)
    assert panel.modify_calls == []


@pytest.mark.asyncio
async def test_remaining_is_never_negative_after_overuse(
    session: AsyncSession,
) -> None:
    account = await _account(session)
    panel = FakePanel(expire=None, data_limit=50 * GB, used=80 * GB)
    snap = await get_snapshot(session, panel, account_id=account.id)  # type: ignore[arg-type]
    assert snap.remaining_bytes == 0


# --------------------------------------------------------------------------- #
# Disable / enable
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disable_then_enable_round_trips(session: AsyncSession) -> None:
    """Cutting a service off must be one tap to undo, so it disables rather
    than deletes — usage, expiry and the subscription link all survive."""
    account = await _account(session)
    panel = FakePanel(expire=int(NOW.timestamp()), data_limit=50 * GB)

    off = await set_enabled(session, panel, account_id=account.id, enabled=False)  # type: ignore[arg-type]
    assert off.status == "disabled"
    assert panel.disable_calls == [True]

    on = await set_enabled(session, panel, account_id=account.id, enabled=True)  # type: ignore[arg-type]
    assert on.status == "active"
    assert panel.disable_calls == [True, False]
    assert panel.expire == int(NOW.timestamp())
    assert panel.data_limit == 50 * GB


@pytest.mark.asyncio
async def test_a_missing_account_is_an_explicit_error(session: AsyncSession) -> None:
    panel = FakePanel(expire=None, data_limit=None)
    with pytest.raises(ServiceOpError):
        await get_snapshot(session, panel, account_id=999_999)  # type: ignore[arg-type]
