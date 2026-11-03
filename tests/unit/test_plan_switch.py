"""Another plan onto the same service: same account, same link, reset.

Owner's decision (2026-09-26): when the plan a service was bought as is gone —
every account imported from the old bot — or the customer wants a different
product (unlimited direct → tunnel volume), they pick any plan and it is
applied to *this* account. Expiry from today, a fresh volume, the new plan's
panel group and device limit; the leftover is dropped, and the summary says so
before anything is paid.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserCreate, UserModify, UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from bot.handlers import purchase
from bot.keyboards.purchase import PurchaseCB, confirm_keyboard
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.store import StorePackage
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.provisioning import ProvisionError, provision_order
from tests.unit.test_renew_same_plan import (
    _bought,
    _catalog,
    _Message,
    _settings,
    _state,
)

GB = 1024**3
DAY = 86_400


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


class _Panel:
    """One panel user that changes the way PasarGuard would."""

    def __init__(self, *, expire_in_days: int, data_limit: int, used: int, hwid: int) -> None:
        self.user = UserResponse(
            id=9,
            username="reza_de",
            status=UserStatus.ACTIVE,
            used_traffic=used,
            created_at=datetime.now(UTC),
            expire=int((datetime.now(UTC) + timedelta(days=expire_in_days)).timestamp()),
            data_limit=data_limit,
            group_ids=[11],
            hwid_limit=hwid,
            subscription_url="https://panel.example.com/sub/reza_de",
        )
        self.calls: list[str] = []
        self.lose_reply: set[str] = set()
        self.created: list[UserCreate] = []
        self.client = AsyncMock()
        self.client.get_user = AsyncMock(side_effect=self._get)
        self.client.reset_user_data_usage = AsyncMock(side_effect=self._reset)
        self.client.modify_user = AsyncMock(side_effect=self._modify)
        self.client.create_user = AsyncMock(side_effect=self._create)
        self.client.subscription_url_from_response = lambda u: u.subscription_url

    async def _get(self, username: str) -> UserResponse:
        self.calls.append("get")
        if username != self.user.username:
            raise PasarGuardNotFoundError("missing", status_code=404)
        return self.user

    async def _reset(self, username: str) -> UserResponse:
        assert username == self.user.username
        self.calls.append("reset")
        self.user = self.user.model_copy(update={"used_traffic": 0})
        if "reset" in self.lose_reply:
            self.lose_reply.discard("reset")
            raise TimeoutError("reply lost after the panel reset")
        return self.user

    async def _modify(self, username: str, payload: UserModify) -> UserResponse:
        assert username == self.user.username
        self.calls.append("modify")
        self.user = self.user.model_copy(
            update={
                k: v
                for k, v in payload.model_dump(exclude_none=True).items()
                if k in {"expire", "data_limit", "hwid_limit", "group_ids"}
            }
        )
        if "modify" in self.lose_reply:
            self.lose_reply.discard("modify")
            raise TimeoutError("reply lost after the panel applied it")
        return self.user

    async def _create(self, payload: UserCreate) -> UserResponse:
        self.calls.append("create")
        self.created.append(payload)
        return self.user.model_copy(update={"id": 99, "username": payload.username})


async def _tunnel_plan(session: AsyncSession, *, gb: int, days: int, hwid: int) -> Plan:
    plan = Plan(
        name=f"volume-{gb}GB-{days}d",
        description="tunnel",
        data_limit_gb=gb,
        duration_days=days,
        price_toman=500_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[22],
        hwid_limit=hwid,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _approved_switch(
    session: AsyncSession, user: User, account: PasarGuardAccount, plan: Plan
) -> Order:
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            original_price=500_000,
            discount_amount=0,
            final_price=500_000,
            currency="IRT",
            meta={},
        ),
        renew_account_id=account.id,
        renew_resets=True,
    )
    order.status = OrderStatus.PAYMENT_APPROVED
    await session.commit()
    return order


def _expire_unix(panel: _Panel) -> int:
    raw = panel.user.expire
    return int(raw.timestamp()) if isinstance(raw, datetime) else int(raw or 0)


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_switch_rebuilds_the_same_account_on_the_new_plan(session: AsyncSession) -> None:
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=25, data_limit=0, used=40 * GB, hwid=2)
    plan = await _tunnel_plan(session, gb=100, days=90, hwid=0)
    order = await _approved_switch(session, user, account, plan)

    got, _ = await provision_order(session, panel.client, order_id=order.id)

    assert got.id == account.id and got.panel_username == "reza_de", "same account and link"
    assert panel.calls == ["get", "reset", "modify"]
    assert panel.created == []
    assert panel.user.used_traffic == 0
    assert panel.user.data_limit == 100 * GB, "a fresh volume, not leftover + 100"
    assert panel.user.group_ids == [22]
    assert panel.user.hwid_limit == 0, "the new plan's device limit, not the old one's"
    now = int(datetime.now(UTC).timestamp())
    assert abs(_expire_unix(panel) - (now + 90 * DAY)) < 60, "from today, not from old expiry"
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_switch_to_a_shorter_plan_still_applies(session: AsyncSession) -> None:
    """An extension skips when expiry ≥ target. A switch may lower it.

    Same volume on both sides, so only the expiry tells applied from not.
    """
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=60, data_limit=10 * GB, used=0, hwid=1)
    plan = await _tunnel_plan(session, gb=10, days=30, hwid=0)
    order = await _approved_switch(session, user, account, plan)

    await provision_order(session, panel.client, order_id=order.id)

    assert "modify" in panel.calls
    now = int(datetime.now(UTC).timestamp())
    assert abs(_expire_unix(panel) - (now + 30 * DAY)) < 60


@pytest.mark.asyncio
async def test_a_same_size_switch_to_another_panel_group_is_written(
    session: AsyncSession,
) -> None:
    """Bought minutes ago, same days and volume: only the panel group differs."""
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=30, data_limit=50 * GB, used=0, hwid=0)
    plan = await _tunnel_plan(session, gb=50, days=30, hwid=0)
    order = await _approved_switch(session, user, account, plan)

    await provision_order(session, panel.client, order_id=order.id)

    assert panel.calls == ["get", "reset", "modify"]
    assert panel.user.group_ids == [22], "the account stayed on the group the customer left"


@pytest.mark.asyncio
async def test_a_same_size_switch_to_another_device_limit_is_written(
    session: AsyncSession,
) -> None:
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=30, data_limit=50 * GB, used=0, hwid=0)
    panel.user = panel.user.model_copy(update={"group_ids": [22]})
    plan = await _tunnel_plan(session, gb=50, days=30, hwid=2)
    order = await _approved_switch(session, user, account, plan)

    await provision_order(session, panel.client, order_id=order.id)

    assert "modify" in panel.calls
    assert panel.user.hwid_limit == 2


@pytest.mark.asyncio
async def test_a_lost_modify_reply_is_not_applied_twice(session: AsyncSession) -> None:
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=25, data_limit=0, used=5 * GB, hwid=1)
    plan = await _tunnel_plan(session, gb=50, days=60, hwid=0)
    order = await _approved_switch(session, user, account, plan)
    panel.lose_reply.add("modify")

    with pytest.raises(ProvisionError, match="panel_transient"):
        await provision_order(session, panel.client, order_id=order.id)
    await session.rollback()
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING
    target = order.renew_target_expire_unix
    assert target is not None, "the target was committed before the panel call"

    # The customer uses traffic in between; a replayed reset would wipe it.
    panel.user = panel.user.model_copy(update={"used_traffic": 3 * GB})
    await provision_order(session, panel.client, order_id=order.id)

    assert panel.calls.count("reset") == 1
    assert panel.calls.count("modify") == 1
    assert panel.user.used_traffic == 3 * GB
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert order.renew_target_expire_unix == target


@pytest.mark.asyncio
async def test_a_reset_whose_modify_never_ran_is_finished_on_retry(
    session: AsyncSession,
) -> None:
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=25, data_limit=0, used=5 * GB, hwid=1)
    plan = await _tunnel_plan(session, gb=50, days=60, hwid=0)
    order = await _approved_switch(session, user, account, plan)
    panel.lose_reply.add("reset")
    order_id = order.id

    with pytest.raises(ProvisionError, match="panel_transient"):
        await provision_order(session, panel.client, order_id=order_id)
    await session.rollback()
    await provision_order(session, panel.client, order_id=order_id)

    assert panel.calls.count("modify") == 1
    assert panel.user.data_limit == 50 * GB
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_switch_whose_account_is_not_theirs_creates_a_new_service(
    session: AsyncSession,
) -> None:
    """Never guess another account by plan shape for a switch."""
    germany, _ = await _catalog(session)
    owner, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    stranger = User(telegram_id=777, referral_code="STR777", wallet_balance_toman=0)
    session.add(stranger)
    await session.flush()
    # The stranger's own service has exactly the new plan's shape: a guess by
    # fingerprint would find it and stack onto it.
    session.add(
        PasarGuardAccount(
            user_id=stranger.id,
            panel_user_id=55,
            panel_username="stranger_own",
            plan_fingerprint="volume:50:60",
        )
    )
    await session.flush()
    panel = _Panel(expire_in_days=25, data_limit=0, used=0, hwid=1)
    plan = await _tunnel_plan(session, gb=50, days=60, hwid=0)
    order = await _approved_switch(session, stranger, account, plan)

    await provision_order(session, panel.client, order_id=order.id)

    assert "modify" not in panel.calls and "reset" not in panel.calls
    assert len(panel.created) == 1
    assert owner.id != stranger.id


@pytest.mark.asyncio
async def test_a_later_renew_keeps_the_device_limit_the_switch_set(
    session: AsyncSession,
) -> None:
    """Renew sends the limit the service was built with — after a switch, the new one."""
    from services.provisioning import _hwid_limit_service_was_created_with

    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    panel = _Panel(expire_in_days=25, data_limit=0, used=0, hwid=1)
    plan = await _tunnel_plan(session, gb=50, days=60, hwid=3)
    order = await _approved_switch(session, user, account, plan)
    await provision_order(session, panel.client, order_id=order.id)

    assert (
        await _hwid_limit_service_was_created_with(session, account=account, order=order, plan=plan)
        == 3
    )


@pytest.mark.asyncio
async def test_a_reset_with_no_account_to_reset_is_not_stored(session: AsyncSession) -> None:
    user = User(telegram_id=779, referral_code="STR779", wallet_balance_toman=0)
    session.add(user)
    await session.flush()
    plan = await _tunnel_plan(session, gb=10, days=30, hwid=0)
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=PriceResult(
            original_price=1, discount_amount=0, final_price=1, currency="IRT", meta={}
        ),
        renew_resets=True,
    )
    assert order.renew_resets is False


# --------------------------------------------------------------------------- #
# The customer's screens
# --------------------------------------------------------------------------- #


def _callbacks(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_a_renew_summary_offers_another_plan_for_the_same_service() -> None:
    assert f"{PurchaseCB.SWITCH_PREFIX}42" in _callbacks(confirm_keyboard(switch_account_id=42))
    assert not any(cb.startswith(PurchaseCB.SWITCH_PREFIX) for cb in _callbacks(confirm_keyboard()))


class _Recorder(_Message):
    def __init__(self) -> None:
        super().__init__()
        self.markups: list[Any] = []

    async def answer(self, text: str, reply_markup: object = None) -> None:
        self.texts.append(text)
        self.markups.append(reply_markup)


@pytest.mark.asyncio
async def test_the_whole_switch_from_a_gone_plan_to_a_pending_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renew a service whose plan is gone, pick another group's plan, confirm."""
    germany, turkey = await _catalog(session)
    package = (
        await session.execute(select(StorePackage).where(StorePackage.slug == "de-30"))
    ).scalar_one()
    package.enabled = False
    await session.commit()
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    ctx = await _state()
    message = _Recorder()
    settings = _settings()

    await purchase.start_renew_purchase(
        message, ctx, session, settings, user, account_id=account.id  # type: ignore[arg-type]
    )
    assert message.texts[-1] == texts.purchase_switch_plan_gone

    tr = next(
        s for s in (await session.execute(select(StorePackage))).scalars() if s.slug == "tr-30"
    )
    cb = SimpleNamespace(
        answer=AsyncMock(), message=message, data=f"{PurchaseCB.CAT_PREFIX}{turkey.id}"
    )
    await purchase.choose_category(cb, ctx, session, settings, user)  # type: ignore[arg-type]
    cb.data = f"{PurchaseCB.READY_PREFIX}{tr.id}"
    await purchase.choose_ready_plan(cb, ctx, session, settings, user)  # type: ignore[arg-type]
    assert texts.purchase_enter_service_name not in message.texts, "the service keeps its name"
    assert await ctx.get_state() == PurchaseStates.entering_promo.state

    await purchase.promo_skip(cb, ctx, session, settings, user)  # type: ignore[arg-type]
    summary = message.texts[-1]
    assert texts.purchase_switch_note in summary, "the reset is said before paying"
    assert not any(c.startswith(PurchaseCB.SWITCH_PREFIX) for c in _callbacks(message.markups[-1]))

    monkeypatch.setattr(purchase, "begin_payment_wait", AsyncMock())
    await purchase.confirm_order(cb, ctx, session, settings, user)  # type: ignore[arg-type]
    order = (
        await session.execute(
            select(Order).where(
                Order.user_id == user.id, Order.status == OrderStatus.PENDING_PAYMENT
            )
        )
    ).scalar_one()
    assert order.renew_account_id == account.id
    assert order.renew_resets is True
    assert order.store_package_slug == "tr-30"
    assert order.service_name is None, "the existing name is kept"


@pytest.mark.asyncio
async def test_a_plain_renew_summary_carries_the_switch_button(session: AsyncSession) -> None:
    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    message = _Recorder()
    ctx = await _state()
    await purchase.start_renew_purchase(
        message, ctx, session, _settings(), user, account_id=account.id  # type: ignore[arg-type]
    )
    assert message.texts[-1] == texts.purchase_enter_promo, "a retail renew asks for a code first"
    cb = SimpleNamespace(answer=AsyncMock(), message=message)
    await purchase.promo_skip(cb, ctx, session, _settings(), user)  # type: ignore[arg-type]
    assert f"{PurchaseCB.SWITCH_PREFIX}{account.id}" in _callbacks(message.markups[-1])
    assert texts.purchase_switch_note not in message.texts[-1], "a renew keeps the leftover"


@pytest.mark.asyncio
async def test_someone_elses_account_cannot_be_switched(session: AsyncSession) -> None:
    germany, _ = await _catalog(session)
    _owner, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    stranger = User(telegram_id=778, referral_code="STR778", wallet_balance_toman=0)
    session.add(stranger)
    await session.commit()
    ctx = await _state()
    message = _Recorder()
    cb = SimpleNamespace(
        answer=AsyncMock(), message=message, data=f"{PurchaseCB.SWITCH_PREFIX}{account.id}"
    )
    await purchase.switch_plan_for_account(cb, ctx, session, _settings(), stranger)  # type: ignore[arg-type]

    assert message.texts == [texts.account_error]
    assert await ctx.get_data() == {}
    assert await session.scalar(select(func.count()).select_from(Order)) == 1
