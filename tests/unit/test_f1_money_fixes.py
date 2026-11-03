"""F1 audit fixes that had no proof test of their own (SUSPECT-MONEY-1/2, FLOW-3)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from api_client.models import UserResponse
from bot.handlers import admin as admin_handlers
from bot.keyboards.admin import AdminCB
from bot.keyboards.admin_payment import PayCB
from bot.keyboards.nav import ADMIN_HOME_CB
from database.base import Base
from database.models import Order, OrderStatus, User, WalletLedgerEntry
from services.wallet import get_balance, retry_debit_key
from tests.unit.test_admin_commercial_flow import (
    ADMIN_ID,
    PRICE,
    _AcceptingPanel,
    _admin,
    _callback,
    _card_order,
    _patch_panel,
    _settings,
    _state,
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class _FailingPanel:
    """create_user answers with ``status_code``; the user never exists."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def get_user(self, username: str) -> UserResponse:
        _ = username
        raise PasarGuardNotFoundError("missing")

    async def create_user(self, payload: object) -> UserResponse:
        _ = payload
        raise PasarGuardAPIError("boom", status_code=self.status_code)

    async def aclose(self) -> None:
        return None


async def _approve(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, status: int
) -> tuple[User, Order, SimpleNamespace]:
    _patch_panel(monkeypatch, _FailingPanel(status))
    admin = await _admin(session)
    buyer, order = await _card_order(session)
    bot = SimpleNamespace(send_message=AsyncMock())
    await admin_handlers.pay_approve(
        _callback(f"{PayCB.APPROVE_PREFIX}{order.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        bot,  # type: ignore[arg-type]
        admin,
    )
    await session.refresh(order)
    return buyer, order, bot


@pytest.mark.asyncio
async def test_a_panel_500_on_card_approve_does_not_credit_the_wallet(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUSPECT-MONEY-1: a 500 may follow a successful create, so refunding it
    would pay the customer twice when the retry then delivers."""
    buyer, order, bot = await _approve(session, monkeypatch, 500)

    assert order.status == OrderStatus.PROVISION_FAILED
    assert await get_balance(session, buyer.id) == 0
    ledger = (await session.execute(select(WalletLedgerEntry))).scalars().all()
    assert ledger == []
    # The customer is never told about a refund that did not happen; the admin is told.
    recipients = [call.kwargs["chat_id"] for call in bot.send_message.await_args_list]
    assert buyer.telegram_id not in recipients
    assert recipients
    # AUDIT-FLOW-3: the admin DM is not a dead end.
    admin_call = bot.send_message.await_args_list[0]
    callbacks = [
        b.callback_data for row in admin_call.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert f"{AdminCB.ORDER}{order.id}" in callbacks
    assert ADMIN_HOME_CB in callbacks


@pytest.mark.asyncio
async def test_a_panel_422_on_card_approve_still_credits_the_wallet(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    buyer, order, bot = await _approve(session, monkeypatch, 422)

    assert order.status == OrderStatus.PROVISION_FAILED
    assert await get_balance(session, buyer.id) == PRICE
    recipients = [call.kwargs["chat_id"] for call in bot.send_message.await_args_list]
    assert buyer.telegram_id in recipients


@pytest.mark.asyncio
async def test_a_failed_admin_retry_does_not_keep_the_retry_debit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUSPECT-MONEY-2: the retry charges the refunded wallet before provisioning.
    A panel 500 is not an AdminError, so the handler used to commit that charge
    with nothing delivered."""
    buyer, order, _bot = await _approve(session, monkeypatch, 422)  # refunded: wallet == PRICE
    buyer_id, order_id = buyer.id, order.id
    assert await get_balance(session, buyer_id) == PRICE
    admin = await session.scalar(select(User).where(User.telegram_id == ADMIN_ID))

    _patch_panel(monkeypatch, _FailingPanel(500))
    await admin_handlers.admin_retry_provision(
        _callback(f"{AdminCB.RETRY}{order_id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(send_message=AsyncMock()),  # type: ignore[arg-type]
        admin,
        _state(),
    )

    await session.refresh(order)
    assert order.status == OrderStatus.PROVISION_FAILED
    assert await get_balance(session, buyer_id) == PRICE
    debits = (
        (
            await session.execute(
                select(WalletLedgerEntry).where(
                    WalletLedgerEntry.idempotency_key == retry_debit_key(order_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert debits == []

    # The next retry, with a healthy panel, charges once and delivers.
    _patch_panel(monkeypatch, _AcceptingPanel())
    await admin_handlers.admin_retry_provision(
        _callback(f"{AdminCB.RETRY}{order_id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(send_message=AsyncMock()),  # type: ignore[arg-type]
        admin,
        _state(),
    )
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert await get_balance(session, buyer_id) == 0


@pytest.mark.asyncio
async def test_a_plan_switch_to_a_shorter_plan_moves_the_earlier_order_too(
    session: AsyncSession,
) -> None:
    """AUDIT-PROV-2, switch side: expiry resets to today + days, which can be
    earlier than the old order's date, so the sibling update must not be 'later only'."""
    from datetime import UTC, datetime, timedelta

    from database.models import Plan
    from database.models.plan import DataLimitResetStrategy
    from services.provisioning import provision_order
    from tests.unit.test_audit_provisioning import (
        _owned_service,
        _panel_user,
        _renewal_order,
        _StatefulPanel,
    )

    customer, _plan, account, first = await _owned_service(session, expires_in=timedelta(days=25))
    short = Plan(
        name="volume-5GB-7d",
        description="audit",
        data_limit_gb=5,
        duration_days=7,
        price_toman=40_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=2,
        is_active=True,
    )
    session.add(short)
    await session.flush()
    expire = int((datetime.now(UTC) + timedelta(days=25)).timestamp())
    panel = _StatefulPanel(_panel_user("alice_a1b2", expire=expire, limit_gb=10, used_gb=1))
    panel.client.reset_user_data_usage = AsyncMock()
    switch = await _renewal_order(session, customer, short)
    switch.renew_resets = True
    switch.renew_account_id = account.id
    switch.delivery_data_limit_gb = 5
    switch.delivery_duration_days = 7
    await session.flush()

    await provision_order(session, panel.client, order_id=switch.id, default_group_ids=[1])
    await session.refresh(first)
    await session.refresh(switch)

    assert switch.status == OrderStatus.ACTIVE
    assert first.expires_at is not None and switch.expires_at is not None
    assert first.expires_at.replace(tzinfo=UTC) == switch.expires_at.replace(tzinfo=UTC)
    assert first.expires_at.replace(tzinfo=UTC) < datetime.now(UTC) + timedelta(days=8)
