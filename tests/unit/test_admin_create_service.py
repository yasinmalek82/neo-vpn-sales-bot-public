"""«🎁 ساخت سرویس برای مشتری»: the owner creates a service for a customer.

The money rules pinned here: a gift is a real ACTIVE order at price 0; a wallet
charge debits exactly once under the checkout debit key; the same request key is
the same order however many times (or however concurrently) it arrives; and a
panel rejection refunds through the same door a customer's own purchase uses.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from bot.config import Settings
from bot.handlers import admin as ha
from bot.keyboards.admin import AdminCB
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, User
from database.models.wallet import WalletLedgerEntry
from services import admin_orders
from services.admin_orders import (
    AdminServiceError,
    create_service_for_customer,
    published_catalog_view,
    sellable_groups,
    wallet_option_available,
)
from services.orders import create_pending_order, get_or_create_dynamic_plan
from services.pricing import PlanType, PriceResult
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    set_package_hwid_limit,
    update_draft_category,
)
from services.wallet import checkout_debit_key, credit, get_balance

ADMIN_TG = 4242
KEY = "a1b2c3d4"
PRICE = 100_000
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    DEFAULT_PANEL_GROUP_IDS=[900],
    ADMIN_TELEGRAM_IDS=[ADMIN_TG],
    STORE_OWNER_TELEGRAM_IDS=[ADMIN_TG],
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


class Shelf:
    """Volume group (panel group 11, plan 50 GB / 30 d / 3 devices) plus a group
    with no panel group."""

    volume_slug = ""
    unlimited_slug = ""
    orphan_slug = ""
    volume_cat_id = 0
    orphan_cat_id = 0


async def _shelf(session: AsyncSession) -> Shelf:
    shelf = Shelf()
    volume = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await update_draft_category(session, volume.id, panel_group_ids=[11])
    unlimited = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await update_draft_category(session, unlimited.id, panel_group_ids=[77])
    orphan = await create_draft_category(session, title_fa="بی‌پنل", kind="volume")
    vol = await create_draft_package(
        session,
        category_id=volume.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=PRICE,
        volume_gb=50,
        duration_days=30,
        hwid_limit=3,
    )
    unl = await create_draft_package(
        session,
        category_id=unlimited.id,
        title_fa="۲ کاربره",
        fixed_price_toman=250_000,
        concurrent_users=2,
        duration_months=1,
    )
    orph = await create_draft_package(
        session,
        category_id=orphan.id,
        title_fa="۱۰ گیگ",
        fixed_price_toman=40_000,
        volume_gb=10,
        duration_days=30,
    )
    shelf.volume_slug, shelf.unlimited_slug, shelf.orphan_slug = (
        str(vol.slug),
        str(unl.slug),
        str(orph.slug),
    )
    await commit_and_activate(session, notes="seed")
    view = await published_catalog_view(session)
    shelf.volume_cat_id = next(c.id for c in view.categories if c.slug == volume.slug)
    shelf.orphan_cat_id = next(c.id for c in view.categories if c.slug == orphan.slug)
    return shelf


async def _customer(
    session: AsyncSession, *, balance: int = 0, reseller: bool = False, tg: int = 500
) -> int:
    user = User(
        telegram_id=tg,
        username=f"u{tg}",
        first_name="Reza",
        language_code="fa",
        referral_code=f"R{tg:07d}",
        is_admin=False,
        is_reseller=reseller,
    )
    session.add(user)
    await session.flush()
    if balance:
        await credit(
            session,
            user_id=user.id,
            amount_toman=balance,
            reason="admin_manual_credit",
            idempotency_key=f"seed:{tg}",
        )
    await session.commit()
    return int(user.id)


def _panel_user(username: str) -> UserResponse:
    return UserResponse(
        id=42,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=50 * 1024**3,
    )


def _client() -> AsyncMock:
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url
    return client


async def _create(
    session: AsyncSession,
    client: AsyncMock,
    shelf: Shelf,
    user_id: int,
    *,
    charge: bool = False,
    key: str = KEY,
    name: str = "reza",
    slug: str | None = None,
) -> PasarGuardAccount:
    return await create_service_for_customer(
        session,
        client,  # type: ignore[arg-type]
        SETTINGS,
        user_id=user_id,
        package_slug=slug or shelf.volume_slug,
        service_name=name,
        charge_wallet=charge,
        request_key=key,
    )


async def _orders(session: AsyncSession) -> list[Order]:
    return list((await session.execute(select(Order).order_by(Order.id))).scalars().all())


async def _ledger(session: AsyncSession) -> list[WalletLedgerEntry]:
    result = await session.execute(
        select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key.like("checkout_debit:%"))
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# Gift
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gift_is_active_at_zero_on_the_groups_panel_groups(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    client = _client()

    account = await _create(session, client, shelf, uid)

    (order,) = await _orders(session)
    assert order.status is OrderStatus.ACTIVE
    assert (int(order.price_toman), int(order.wallet_applied_toman)) == (0, 0)
    assert order.admin_request_key == KEY
    assert order.force_new is True
    assert order.delivery_duration_days == 30
    assert order.store_package_slug == shelf.volume_slug
    payload = client.create_user.await_args.args[0]
    assert list(payload.group_ids) == [11]  # the store group's own, not the env default
    assert payload.hwid_limit == 3  # the plan's option, not `or 1`
    assert account.user_id == uid
    assert await _ledger(session) == []  # a gift touches no wallet
    assert await get_balance(session, uid) == 0


@pytest.mark.asyncio
async def test_unlimited_plan_sends_its_concurrent_users_as_device_limit(
    session: AsyncSession,
) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    client = _client()
    await _create(session, client, shelf, uid, slug=shelf.unlimited_slug)
    payload = client.create_user.await_args.args[0]
    assert list(payload.group_ids) == [77]
    assert payload.hwid_limit == 2


@pytest.mark.asyncio
async def test_volume_plan_with_zero_devices_stays_zero(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    view = await published_catalog_view(session)
    pkg_id = next(p.id for p in view.packages if p.slug == shelf.volume_slug)
    await set_package_hwid_limit(session, pkg_id, hwid_limit=0)
    await commit_and_activate(session, notes="نامحدود")
    uid = await _customer(session)
    client = _client()
    await _create(session, client, shelf, uid)
    assert client.create_user.await_args.args[0].hwid_limit == 0


# --------------------------------------------------------------------------- #
# Wallet
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wallet_charge_debits_once_under_the_checkout_key(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    client = _client()

    await _create(session, client, shelf, uid, charge=True)

    (order,) = await _orders(session)
    assert order.status is OrderStatus.ACTIVE
    assert (int(order.price_toman), int(order.wallet_applied_toman)) == (PRICE, PRICE)
    entries = await _ledger(session)
    assert [e.idempotency_key for e in entries] == [checkout_debit_key(order.id)]
    assert int(entries[0].amount_toman) == PRICE
    assert await get_balance(session, uid) == 200_000 - PRICE


@pytest.mark.asyncio
async def test_wallet_short_creates_nothing(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=50_000)
    client = _client()
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, charge=True)
    assert err.value.code == "wallet_short"
    assert await _orders(session) == []
    assert await get_balance(session, uid) == 50_000
    client.create_user.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_same_key_replay_makes_no_second_order_or_debit(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    client = _client()

    first = await _create(session, client, shelf, uid, charge=True)
    second = await _create(session, client, shelf, uid, charge=True)

    assert second.id == first.id
    assert len(await _orders(session)) == 1
    assert len(await _ledger(session)) == 1
    assert await get_balance(session, uid) == 200_000 - PRICE
    assert client.create_user.await_count == 1
    count = await session.scalar(select(func.count()).select_from(PasarGuardAccount))
    assert count == 1


@pytest.mark.asyncio
async def test_a_racing_call_that_lost_the_insert_resumes_the_winner(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second call's key lookup ran before the first committed; its insert
    hits the unique key. It must land on the winner, not create or charge again."""
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    client = _client()
    first = await _create(session, client, shelf, uid, charge=True)

    real = admin_orders.find_order_by_key
    calls = {"n": 0}

    async def stale_first_lookup(s: AsyncSession, key: str) -> Order | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(s, key)

    monkeypatch.setattr(admin_orders, "find_order_by_key", stale_first_lookup)
    second = await _create(session, client, shelf, uid, charge=True)

    assert calls["n"] >= 2  # it really went through the insert-collision path
    assert second.id == first.id
    assert len(await _orders(session)) == 1
    assert len(await _ledger(session)) == 1
    assert await get_balance(session, uid) == 200_000 - PRICE
    assert client.create_user.await_count == 1


@pytest.mark.asyncio
async def test_a_key_minted_for_another_customer_is_refused(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    other = await _customer(session, tg=501)
    client = _client()
    await _create(session, client, shelf, uid)
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, other)
    assert err.value.code == "key_conflict"
    assert len(await _orders(session)) == 1


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_customer_with_an_open_order_is_refused(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    price = PriceResult(
        original_price=PRICE,
        discount_amount=0,
        final_price=PRICE,
        currency="IRT",
        meta={"volume_gb": 10, "duration_days": 30},
    )
    plan = await get_or_create_dynamic_plan(
        session, plan_type=PlanType.VOLUME, price=price, group_ids=[11], hwid_limit=0
    )
    mine = await create_pending_order(session, user_id=uid, plan=plan, price=price)
    await session.commit()
    client = _client()

    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, charge=True, key="0f0f0f0f")

    assert err.value.code == "open_order"
    orders = await _orders(session)
    assert [o.id for o in orders] == [mine.id]  # theirs is untouched, none was added
    assert orders[0].admin_request_key is None
    assert await _ledger(session) == []
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsellable_and_off_sale_groups_are_not_offered_or_created(
    session: AsyncSession,
) -> None:
    shelf = await _shelf(session)
    view = await published_catalog_view(session)
    offered = {c.id for c in sellable_groups(view)}
    assert shelf.volume_cat_id in offered
    assert shelf.orphan_cat_id not in offered  # no panel group anywhere

    uid = await _customer(session)
    client = _client()
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, slug=shelf.orphan_slug)
    assert err.value.code == "group_off_sale"

    await update_draft_category(session, shelf.volume_cat_id, enabled=False)
    await commit_and_activate(session, notes="خاموش")
    view = await published_catalog_view(session)
    assert shelf.volume_cat_id not in {c.id for c in sellable_groups(view)}
    with pytest.raises(AdminServiceError) as err2:
        await _create(session, client, shelf, uid, key="11111111")
    assert err2.value.code == "group_off_sale"
    assert await _orders(session) == []
    client.create_user.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Reseller
# --------------------------------------------------------------------------- #


def test_wallet_option_rule() -> None:
    assert wallet_option_available(balance_toman=PRICE, retail_toman=PRICE, is_reseller=False)
    assert not wallet_option_available(
        balance_toman=PRICE - 1, retail_toman=PRICE, is_reseller=False
    )
    assert not wallet_option_available(
        balance_toman=10 * PRICE, retail_toman=PRICE, is_reseller=True
    )


@pytest.mark.asyncio
async def test_reseller_wallet_charge_is_refused_but_gift_works(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=500_000, reseller=True)
    client = _client()
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, charge=True)
    assert err.value.code == "reseller_wallet"
    assert await get_balance(session, uid) == 500_000
    await _create(session, client, shelf, uid, key="22222222")
    (order,) = await _orders(session)
    assert int(order.price_toman) == 0


# --------------------------------------------------------------------------- #
# Name
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_name_is_sanitized_and_names_the_customers_service(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    client = _client()
    account = await _create(session, client, shelf, uid, name="  Reza Ali!! ")
    (order,) = await _orders(session)
    assert order.service_name == "reza_ali"
    assert client.create_user.await_args.args[0].username.startswith("reza_ali_")
    # what «سرویس‌های من» shows
    assert account.service_name == "reza_ali"
    assert account.display_name == "reza_ali"


@pytest.mark.asyncio
async def test_a_persian_only_name_is_refused_before_anything_is_written(
    session: AsyncSession,
) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    client = _client()
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, name="رضا")
    assert err.value.code == "invalid_service_name"
    assert await _orders(session) == []


# --------------------------------------------------------------------------- #
# Provisioning failure on the wallet path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_panel_rejection_refunds_the_wallet_charge(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    client = _client()
    client.create_user = AsyncMock(side_effect=PasarGuardAPIError("bad", status_code=422))

    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, charge=True)

    assert err.value.code == "provision_failed_refunded"
    assert err.value.refunded_toman == PRICE
    assert await get_balance(session, uid) == 200_000  # whole again
    (order,) = await _orders(session)
    assert order.status is OrderStatus.PROVISION_FAILED
    assert len(await _ledger(session)) == 1  # the debit stays in the book, the credit offsets it


@pytest.mark.asyncio
async def test_unproven_failure_keeps_the_debit_and_the_same_key_resumes(
    session: AsyncSession,
) -> None:
    """A 500 may follow a successful panel write, so it must not refund; the
    retry (same key) resumes the same order and bills nothing more."""
    shelf = await _shelf(session)
    uid = await _customer(session, balance=200_000)
    client = _client()
    client.create_user = AsyncMock(side_effect=PasarGuardAPIError("boom", status_code=500))
    with pytest.raises(AdminServiceError) as err:
        await _create(session, client, shelf, uid, charge=True)
    assert err.value.code == "provision_failed"
    assert await get_balance(session, uid) == 200_000 - PRICE

    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    account = await _create(session, client, shelf, uid, charge=True)
    assert account.user_id == uid
    (order,) = await _orders(session)
    assert order.status is OrderStatus.ACTIVE
    assert await get_balance(session, uid) == 200_000 - PRICE
    assert len(await _ledger(session)) == 1


# --------------------------------------------------------------------------- #
# The screens
# --------------------------------------------------------------------------- #


def _buttons(markup: Any) -> list[Any]:
    return [b for row in markup.inline_keyboard for b in row]


async def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_TG, user_id=ADMIN_TG)
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, answer=AsyncMock(), from_user=SimpleNamespace(id=ADMIN_TG), edit_text=AsyncMock()
    )


async def _confirm_screen(
    session: AsyncSession, shelf: Shelf, uid: int
) -> tuple[SimpleNamespace, FSMContext]:
    state = await _fsm()
    await state.set_state(AdminStates.waiting_gift_service_name)
    await state.update_data(gift_user_id=uid, gift_slug=shelf.volume_slug)
    msg = _typed("Reza Ali")
    await ha.admin_gift_name(msg, state, session, SETTINGS, None)  # type: ignore[arg-type]
    return msg, state


@pytest.mark.asyncio
async def test_reseller_gets_no_wallet_button_on_the_confirm_screen(session: AsyncSession) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, balance=500_000, reseller=True)
    msg, _ = await _confirm_screen(session, shelf, uid)
    markup = msg.answer.await_args.kwargs["reply_markup"]
    data = [b.callback_data for b in _buttons(markup)]
    assert any(d.startswith(f"{AdminCB.CUST_GIFT_GO}{uid}:g:") for d in data)
    assert not any(":w:" in d for d in data)


@pytest.mark.asyncio
async def test_wallet_button_needs_enough_balance_and_shows_the_cleaned_name(
    session: AsyncSession,
) -> None:
    shelf = await _shelf(session)
    rich = await _customer(session, balance=PRICE)
    msg, _ = await _confirm_screen(session, shelf, rich)
    data = [b.callback_data for b in _buttons(msg.answer.await_args.kwargs["reply_markup"])]
    assert any(d.startswith(f"{AdminCB.CUST_GIFT_GO}{rich}:w:") for d in data)
    body = msg.answer.await_args.args[0]
    assert "reza_ali" in body and "Reza Ali" in body  # the cleaned result is shown

    poor = await _customer(session, balance=PRICE - 1, tg=502)
    msg2, _ = await _confirm_screen(session, shelf, poor)
    data2 = [b.callback_data for b in _buttons(msg2.answer.await_args.kwargs["reply_markup"])]
    assert not any(":w:" in d for d in data2)


@pytest.mark.asyncio
async def test_start_screen_lists_only_sellable_groups(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    cb = SimpleNamespace(
        data=f"{AdminCB.CUST_GIFT}{uid}", answer=AsyncMock(), from_user=SimpleNamespace(id=ADMIN_TG)
    )
    await ha.admin_gift_start(cb, await _fsm(), session, SETTINGS, None)  # type: ignore[arg-type]
    markup = screen.edit_text.await_args.kwargs["reply_markup"]
    data = [b.callback_data for b in _buttons(markup)]
    assert f"{AdminCB.CUST_GIFT_GROUP}{uid}:{shelf.volume_cat_id}" in data
    assert not any(d.endswith(f":{shelf.orphan_cat_id}") for d in data)


@pytest.mark.asyncio
async def test_confirm_tap_delivers_to_the_customers_chat_and_a_second_tap_is_harmless(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = await _shelf(session)
    uid = await _customer(session, tg=777)
    msg, state = await _confirm_screen(session, shelf, uid)
    go = next(
        b.callback_data
        for b in _buttons(msg.answer.await_args.kwargs["reply_markup"])
        if ":g:" in b.callback_data
    )
    client = _client()

    @asynccontextmanager
    async def fake_panel(_s: AsyncSession, _settings: Settings) -> AsyncGenerator[Any, None]:
        yield client

    deliver = AsyncMock(return_value=True)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_panel", fake_panel)
    monkeypatch.setattr(ha, "deliver_provisioned_service_card", deliver)
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    monkeypatch.setattr(ha, "_show_customer", AsyncMock())

    def tap() -> SimpleNamespace:
        return SimpleNamespace(data=go, answer=AsyncMock(), from_user=SimpleNamespace(id=ADMIN_TG))

    bot = SimpleNamespace()
    await ha.admin_gift_go(tap(), state, session, SETTINGS, bot, None)  # type: ignore[arg-type]
    assert deliver.await_args.kwargs["chat_id"] == 777
    assert deliver.await_args.kwargs["intro"] == texts.notify_admin_service_created
    assert len(await _orders(session)) == 1

    # State was cleared on success: the stale button says "done", makes nothing.
    await ha.admin_gift_go(tap(), state, session, SETTINGS, bot, None)  # type: ignore[arg-type]
    assert len(await _orders(session)) == 1
    assert client.create_user.await_count == 1


def test_callbacks_fit_and_do_not_collide() -> None:
    values = [v for k, v in vars(AdminCB).items() if k.isupper() and isinstance(v, str)]
    new = [
        AdminCB.CUST_GIFT,
        AdminCB.CUST_GIFT_GROUP,
        AdminCB.CUST_GIFT_PLAN,
        AdminCB.CUST_GIFT_GO,
    ]
    for prefix in new:
        others = [v for v in values if v != prefix]
        assert not any(o.startswith(prefix) or prefix.startswith(o) for o in others), prefix
    worst = f"{AdminCB.CUST_GIFT_GO}{2**40}:w:{KEY}"
    assert len(worst.encode()) <= 64
    assert len(f"{AdminCB.CUST_GIFT_PLAN}{2**40}:{2**40}".encode()) <= 64
