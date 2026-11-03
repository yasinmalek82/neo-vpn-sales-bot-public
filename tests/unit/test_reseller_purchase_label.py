"""A reseller buying a service names their customer, not the panel account.

Driven through the real handlers, because the whole defect class here lives in
the wiring: a correct `build_label` is worthless if the checkout still asks the
shop for an English word, or if the label it collects never reaches the order.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers.customer_label import customer_name_entered, customer_phone_entered
from bot.handlers.purchase import _ask_service_name
from bot.states import PurchaseStates, ResellerStates
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.pasarguard_account import PasarGuardAccount
from database.models.plan import DataLimitResetStrategy
from services.pricing import PlanType
from services.provisioning import provision_order

SETTINGS = SimpleNamespace(service_expire_warn_days=7, trial_enabled=True)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Stand in for the summary screen: record that it was reached, and how.

    A reseller is not asked for a promo code, so the step after the name is the
    summary itself. Its content is covered by the e2e scenarios; here only the
    hand-off matters.
    """
    from bot.handlers import purchase

    calls: list[dict[str, object]] = []

    async def fake(_message, _state, _session, _settings, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)

    monkeypatch.setattr(purchase, "_show_summary", fake)
    return calls


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock())


async def _user(session: AsyncSession, tg: int, *, reseller: bool) -> User:
    user = User(telegram_id=tg, referral_code=f"R{tg}", is_reseller=reseller)
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession) -> Plan:
    plan = Plan(
        name="v-50-30",
        data_limit_gb=50,
        duration_days=30,
        price_toman=150_000,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


def _panel_user(username: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=1234,
        username=username,
        expire=datetime.now(UTC) + timedelta(days=30),
        data_limit=50 * 1024**3,
        status="active",
        subscription_url=f"https://panel.example.com/sub/{username}",
    )


# --- which question gets asked --------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_customer_is_still_asked_for_an_english_name(
    session: AsyncSession,
) -> None:
    """They are naming **their own** service, and that name seeds the panel
    username they will see in their client. Nothing about that was broken."""
    customer = await _user(session, 800, reseller=False)
    state = _state()
    message = _typed("")
    await _ask_service_name(message, state, customer)  # type: ignore[arg-type]
    assert await state.get_state() == PurchaseStates.entering_service_name.state
    assert "انگلیسی" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_a_reseller_is_asked_who_the_service_is_for(session: AsyncSession) -> None:
    """Same Latin name, but asked as «who is this for» and followed by a phone."""
    shop = await _user(session, 801, reseller=True)
    state = _state()
    message = _typed("")
    await _ask_service_name(message, state, shop)  # type: ignore[arg-type]
    assert await state.get_state() == ResellerStates.waiting_customer_name.state
    said = message.answer.await_args.args[0]
    assert "کدوم مشتری" in said
    assert "انگلیسی" in said, "it seeds the panel username, so it has to be Latin"


# --- the two answers land on the order ------------------------------------


@pytest.mark.asyncio
async def test_the_label_reaches_checkout_and_the_panel_name_does_not(
    session: AsyncSession, summary_calls: list[dict[str, object]]
) -> None:
    shop = await _user(session, 802, reseller=True)
    await session.commit()

    state = _state()
    await state.update_data(plan_type=PlanType.VOLUME.value, volume_gb=50, duration_days=30)
    await _ask_service_name(_typed(""), state, shop)  # type: ignore[arg-type]
    await customer_name_entered(_typed("Reza Mohammadi"), state)  # type: ignore[arg-type]
    await customer_phone_entered(
        _typed("۰۹۱۲ ۳۴۵ ۶۷۸۹"),  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )

    data = await state.get_data()
    assert data["customer_label"] == "reza_mohammadi | 09123456789"
    # The name seeds the panel username; the phone must never get there.
    assert data["service_name"] == "reza_mohammadi"
    # A reseller is not asked for a promo code: the summary comes next, with none.
    assert await state.get_state() != PurchaseStates.entering_promo.state
    assert [c["promo_code"] for c in summary_calls] == [None]


@pytest.mark.asyncio
async def test_the_order_summary_names_the_customer_not_a_service(
    session: AsyncSession,
) -> None:
    """The shop is about to pay for somebody else's service; «نام سرویس: —» on
    the confirmation tells them nothing about which sale this is."""
    from bot.handlers.purchase import _summary_details

    details = _summary_details(
        {
            "plan_type": PlanType.VOLUME.value,
            "volume_gb": 50,
            "duration_days": 30,
            "customer_label": "reza | 09123456789",
        }
    )
    assert "مشتری: reza | 09123456789" in details
    assert "نام سرویس" not in details


# --- and through provisioning ---------------------------------------------


@pytest.mark.asyncio
async def test_the_panel_name_carries_the_customer_and_never_the_phone(
    session: AsyncSession,
) -> None:
    """The two halves of the owner's request, on one order.

    The panel gets `reza_<random>` — the shop's own customer name plus a random
    suffix, so the same sale is recognisable on both sides. «مشتری‌های من» gets
    «reza | 0912…». The eleven digits stay out of the username.
    """
    shop = await _user(session, 803, reseller=True)
    plan = await _plan(session)
    order = Order(
        user_id=shop.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_APPROVED,
        price_toman=150_000,
        discount_toman=0,
        service_name="reza",
        customer_label="reza | 09123456789",
        paid_at=datetime.now(UTC),
    )
    session.add(order)
    await session.flush()

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=_not_found)
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url

    account, _reward = await provision_order(
        session, client, order_id=order.id, default_group_ids=[1]
    )

    username = account.panel_username or ""
    assert username.startswith("reza_")
    assert len(username.removeprefix("reza_")) == 4, "name, underscore, random suffix"
    assert "09123456789" not in username
    assert account.display_name == "reza | 09123456789"
    assert account.service_name == "reza"


async def _not_found(_username: str) -> None:
    from api_client.exceptions import PasarGuardNotFoundError

    raise PasarGuardNotFoundError("missing", status_code=404)


# --- what the shop actually sees ------------------------------------------


def test_the_list_row_carries_the_shape_the_owner_specified() -> None:
    """«اسم انتخاب شده | شماره تلفن مشتری اگر وارد کرده» — both cases."""
    from bot.keyboards.account import reseller_services_keyboard

    page = SimpleNamespace(
        rows=[
            SimpleNamespace(
                id=1,
                display_name="reza_mohammadi | 09123456789",
                service_name=None,
                panel_username="neo_a7f3k2m1",
            ),
            SimpleNamespace(
                id=2, display_name="karimi", service_name=None, panel_username="neo_b8x2"
            ),
        ],
        has_prev=False,
        has_next=False,
        query=None,
    )
    labels = [b.text for b in reseller_services_keyboard(page).inline_keyboard[0]]
    labels += [b.text for b in reseller_services_keyboard(page).inline_keyboard[1]]
    assert any(t.endswith("reza_mohammadi | 09123456789") for t in labels)
    assert any(t.endswith("karimi") for t in labels)
    assert not any(t.endswith("|") for t in labels), "a customer with no phone gets no separator"
    assert not any("a7f3k2m1" in t for t in labels), "the row is the shop's own note"


def test_the_card_a_shop_opens_names_both_the_customer_and_the_panel() -> None:
    """«اسم مشتریش داخل پنل رو توی کارت مشتری نشان بده».

    Two names, two jobs: the label says whose service it is, the panel username
    is what they search for in PasarGuard when something has to be fixed there.
    """
    from services.accounts import AccountOverview
    from services.service_card import format_overview_text

    overview = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime.now(UTC) + timedelta(days=25),
        used_traffic=2 * 1024**3,
        data_limit=50 * 1024**3,
        subscription_url="https://panel.example.com/sub/neo_a7f3k2m1",
        panel_username="neo_a7f3k2m1",
        display_name="reza_mohammadi | 09123456789",
    )
    shop_view = format_overview_text(overview, show_customer=True)
    assert "مشتری: reza_mohammadi | 09123456789" in shop_view
    assert "neo_a7f3k2m1" in shop_view
    assert "سرویس تو" not in shop_view, "it is their customer's service, not theirs"

    own_view = format_overview_text(overview, show_customer=False)
    assert "مشتری:" not in own_view, "a customer looking at their own service is not a shop"
    assert "سرویس تو" in own_view


# --- the renew question, and the trap behind it ----------------------------


@pytest.mark.asyncio
async def test_a_reseller_is_never_asked_to_renew_or_create(
    session: AsyncSession, summary_calls: list[dict[str, object]]
) -> None:
    """«برای این پلن یه سرویس فعال داری» is unanswerable for a shop.

    Of course they have one — they have thirty, one per customer, and the plan
    shape says nothing about which walk-in is at the counter. The answer is
    always «new», so the question is a tap that teaches nothing.
    """
    from bot.handlers.purchase import _after_service_name

    shop = await _user(session, 810, reseller=True)
    plan = await _plan(session)
    existing = PasarGuardAccount(
        user_id=shop.id,
        panel_user_id=7001,
        panel_username="ali_b8x2",
        display_name="ali | 09120000000",
        plan_fingerprint="volume:50:30",
    )
    session.add(existing)
    order = Order(
        user_id=shop.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=150_000,
        discount_toman=0,
        panel_username="ali_b8x2",
        expires_at=datetime.now(UTC) + timedelta(days=20),
    )
    session.add(order)
    await session.flush()

    state = _state()
    await state.update_data(plan_type=PlanType.VOLUME.value, volume_gb=50, duration_days=30)
    message = _typed("")
    await _after_service_name(
        message,  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
        "reza",
    )
    assert await state.get_state() != PurchaseStates.choosing_renew_mode.state
    assert await state.get_state() != PurchaseStates.entering_promo.state
    assert [c["promo_code"] for c in summary_calls] == [None]
    message.answer.assert_not_awaited()  # neither the renew question nor the promo prompt


@pytest.mark.asyncio
async def test_a_customer_is_still_asked(session: AsyncSession) -> None:
    """For someone with two services of their own the question is the whole
    point: it is how they renew instead of accidentally buying a second."""
    from bot.handlers.purchase import _after_service_name

    customer = await _user(session, 811, reseller=False)
    plan = await _plan(session)
    session.add(
        PasarGuardAccount(
            user_id=customer.id,
            panel_user_id=7002,
            panel_username="myvpn_c3d4",
            display_name="myvpn",
            plan_fingerprint="volume:50:30",
        )
    )
    session.add(
        Order(
            user_id=customer.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=150_000,
            discount_toman=0,
            panel_username="myvpn_c3d4",
            expires_at=datetime.now(UTC) + timedelta(days=20),
        )
    )
    await session.flush()

    state = _state()
    await state.update_data(plan_type=PlanType.VOLUME.value, volume_gb=50, duration_days=30)
    message = _typed("")
    await _after_service_name(
        message,  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        customer,
        "myvpn",
    )
    assert await state.get_state() == PurchaseStates.choosing_renew_mode.state


@pytest.mark.asyncio
async def test_a_second_sale_of_the_same_plan_does_not_extend_the_first_customer(
    session: AsyncSession,
) -> None:
    """The trap the removed question was standing in front of.

    Provisioning matches an existing account by plan fingerprint whenever the
    order does not say otherwise. For a shop that means customer B's order
    lands on customer A's service: A quietly gets a second month, B gets
    nothing, and the shop has paid for it.
    """
    shop = await _user(session, 812, reseller=True)
    plan = await _plan(session)
    first = PasarGuardAccount(
        user_id=shop.id,
        panel_user_id=7003,
        panel_username="ali_b8x2",
        display_name="ali | 09120000000",
        plan_fingerprint="volume:50:30",
    )
    session.add(first)
    session.add(
        Order(
            user_id=shop.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=150_000,
            discount_toman=0,
            panel_username="ali_b8x2",
            expires_at=datetime.now(UTC) + timedelta(days=20),
        )
    )
    second = Order(
        user_id=shop.id,
        plan_id=plan.id,
        status=OrderStatus.PAYMENT_APPROVED,
        price_toman=150_000,
        discount_toman=0,
        service_name="reza",
        customer_label="reza | 09123456789",
        paid_at=datetime.now(UTC),
    )
    session.add(second)
    await session.flush()

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=_not_found)
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.modify_user = AsyncMock()
    client.subscription_url_from_response = lambda u: u.subscription_url

    account, _reward = await provision_order(
        session, client, order_id=second.id, default_group_ids=[1]
    )
    client.modify_user.assert_not_awaited()
    assert account.id != first.id, "customer B must get their own service"
    assert (account.panel_username or "").startswith("reza_")


@pytest.mark.asyncio
async def test_renewing_a_named_customer_does_not_ask_who_it_is_for(
    session: AsyncSession, summary_calls: list[dict[str, object]]
) -> None:
    """They said who by tapping them in «مشتری‌های من».

    Asking again would put whatever they retype into `customer_label`, and
    `_finalize_active` writes that over the existing label — a phone recalled
    from memory is how one customer quietly becomes two.
    """
    shop = await _user(session, 813, reseller=True)
    await session.flush()

    state = _state()
    await state.update_data(
        plan_type=PlanType.VOLUME.value,
        volume_gb=50,
        duration_days=30,
        intent_renew_account_id=77,
        renew_account_id=77,
    )
    message = _typed("")
    await _ask_service_name(
        message,  # type: ignore[arg-type]
        state,
        shop,
        session,
        SETTINGS,  # type: ignore[arg-type]
    )
    assert await state.get_state() != PurchaseStates.entering_promo.state
    assert [c["promo_code"] for c in summary_calls] == [None]
    data = await state.get_data()
    assert data.get("customer_label") is None, "nothing may overwrite the stored label"
    assert data["renew_account_id"] == 77


@pytest.mark.asyncio
async def test_a_fresh_reseller_sale_still_asks(session: AsyncSession) -> None:
    """Guards the branch above from swallowing the ordinary case."""
    shop = await _user(session, 814, reseller=True)
    await session.flush()

    state = _state()
    await state.update_data(plan_type=PlanType.VOLUME.value, volume_gb=50, duration_days=30)
    await _ask_service_name(
        _typed(""),  # type: ignore[arg-type]
        state,
        shop,
        session,
        SETTINGS,  # type: ignore[arg-type]
    )
    assert await state.get_state() == ResellerStates.waiting_customer_name.state
