"""Seeds and a fake panel for the personal pages (``/panel/me/...``) tests.

Every value that identifies a person in a test is distinctive on purpose
(a name, a panel username, a link token, an amount), so an isolation test can
search a page for the *other* person's values and a leak shows.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from bot.config import Settings
from database.models import (
    Order,
    OrderStatus,
    PasarGuardAccount,
    Plan,
    ResellerRate,
    User,
    WalletEntryType,
    WalletLedgerEntry,
)
from database.models.order import TRIAL_KIND_RESELLER
from database.models.plan import DataLimitResetStrategy
from services import personal_page
from tests.unit.web_panel_helpers import panel_client, panel_settings

Factory = async_sessionmaker[AsyncSession]

_ids = itertools.count(1)
GB = 1024**3


async def make_person(
    factory: Factory,
    telegram_id: int,
    *,
    name: str = "کاربر",
    reseller: bool = False,
    banned: bool = False,
    balance: int = 0,
) -> int:
    """A ``User`` row; returns its row id."""
    async with factory() as session:
        user = User(
            telegram_id=telegram_id,
            username=f"u{telegram_id}",
            first_name=name,
            language_code="fa",
            referral_code=f"R{telegram_id}",
            is_reseller=reseller,
            is_banned=banned,
            wallet_balance_toman=balance,
        )
        session.add(user)
        await session.commit()
        return int(user.id)


async def _plan(session: AsyncSession, *, name: str = "p", price: int = 0) -> Plan:
    plan = Plan(
        name=name,
        description="p",
        data_limit_gb=10,
        duration_days=30,
        price_toman=price,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


async def make_service(
    factory: Factory,
    user_id: int,
    *,
    username: str,
    label: str,
    link: str | None = None,
    expires_in_days: int = 20,
    price: int = 0,
) -> int:
    """A named service with an active order; returns the account id.

    The order is free by default so it adds nothing to a sales total.
    """
    n = next(_ids)
    async with factory() as session:
        plan = await _plan(session, name=f"plan-{n}")
        account = PasarGuardAccount(
            user_id=user_id,
            panel_user_id=1000 + n,
            panel_username=username,
            subscription_url=link or f"https://panel.example.com/sub/TOKEN-{username}",
            display_name=label,
        )
        session.add(account)
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                discount_toman=0,
                force_new=True,
                status=OrderStatus.ACTIVE,
                price_toman=price,
                panel_username=username,
                activated_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
            )
        )
        await session.commit()
        return int(account.id)


async def make_sale(
    factory: Factory,
    user_id: int,
    *,
    price: int,
    status: OrderStatus = OrderStatus.ACTIVE,
    activated_at: datetime | None | str = "now",
) -> None:
    """An order, as the finance windows would see it."""
    stamp = datetime.now(UTC) if activated_at == "now" else activated_at
    async with factory() as session:
        plan = await _plan(session, name=f"sale-{next(_ids)}", price=price)
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                discount_toman=0,
                force_new=True,
                status=status,
                price_toman=price,
                activated_at=stamp,
            )
        )
        await session.commit()


async def make_trials(
    factory: Factory, user_id: int, *, count: int, quota: int, price: int = 0
) -> None:
    """``count`` reseller giveaways this month and a rate card with ``quota``."""
    async with factory() as session:
        for _ in range(count):
            plan = await _plan(session, name=f"trial-{next(_ids)}")
            session.add(
                Order(
                    user_id=user_id,
                    plan_id=plan.id,
                    discount_toman=0,
                    force_new=True,
                    status=OrderStatus.ACTIVE,
                    price_toman=0,
                    trial_kind=TRIAL_KIND_RESELLER,
                    activated_at=datetime.now(UTC),
                )
            )
        session.add(
            ResellerRate(user_id=user_id, trial_quota_monthly=quota, trial_price_toman=price)
        )
        await session.commit()


async def make_ledger(
    factory: Factory,
    user_id: int,
    *,
    amount: int,
    credit: bool,
    reason: str,
    after: int,
    note: str | None = None,
) -> None:
    async with factory() as session:
        session.add(
            WalletLedgerEntry(
                user_id=user_id,
                entry_type=WalletEntryType.credit if credit else WalletEntryType.debit,
                amount_toman=amount,
                reason=reason,
                idempotency_key=f"seed-{next(_ids)}",
                balance_after_toman=after,
                note=note,
            )
        )
        await session.commit()


class FakePanel:
    """Stands in for ``PasarGuardClient``: ``get_user`` answers from a dict or raises."""

    def __init__(self, users: dict[str, UserResponse | Exception]) -> None:
        self.users = users
        self.reads: list[str] = []
        self.closed = False

    async def get_user(self, username: str) -> UserResponse:
        self.reads.append(username)
        item = self.users[username]
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


def panel_user(
    username: str,
    *,
    used_gb: float = 2.0,
    limit_gb: int | None = 10,
    expires_in_days: int = 12,
    status: UserStatus = UserStatus.ACTIVE,
) -> UserResponse:
    return UserResponse(
        id=1,
        username=username,
        status=status,
        used_traffic=int(used_gb * GB),
        created_at=datetime.now(UTC),
        expire=datetime.now(UTC) + timedelta(days=expires_in_days),
        data_limit=None if limit_gb is None else limit_gb * GB,
        subscription_url=f"https://panel.example.com/sub/TOKEN-{username}",
    )


def use_panel(monkeypatch: pytest.MonkeyPatch, panel: FakePanel) -> FakePanel:
    async def fake_client(*_a: Any, **_kw: Any) -> FakePanel:
        return panel

    monkeypatch.setattr(personal_page, "get_pasarguard_client", fake_client)
    return panel


async def mark_panel_down(factory: Factory) -> None:
    """Row 0 says down for two checks, just checked: ``is_panel_down`` is true."""
    from database.models.panel_node_state import PANEL_NODE_ID, PanelNodeState

    async with factory() as session:
        session.add(
            PanelNodeState(
                node_id=PANEL_NODE_ID,
                name="panel",
                status="unreachable",
                checks_in_status=3,
                status_since=datetime.now(UTC),
                last_checked_at=datetime.now(UTC),
            )
        )
        await session.commit()


@asynccontextmanager
async def personal_client(
    factory: Factory, *, public: bool = True, settings: Settings | None = None
) -> AsyncIterator[TestClient]:
    """The panel app with ``WEB_PANEL_PUBLIC_ENABLED`` as asked and an empty panel-read cache."""
    personal_page.clear_live_cache()
    async with panel_client(
        factory, settings or panel_settings(WEB_PANEL_PUBLIC_ENABLED=public)
    ) as client:
        yield client
