"""Admin actions that move money or the live catalog, through the handlers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from api_client.models import UserResponse, UserStatus
from bot.handlers import admin as admin_handlers
from bot.handlers import store_admin
from bot.keyboards.admin import AdminCB
from bot.keyboards.admin_payment import PayCB
from bot.keyboards.store_admin import StoreCB
from database.base import Base
from database.models import Order, OrderStatus, Payment, PaymentStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.checkout import quote_checkout
from services.orders import create_pending_order
from services.payments import submit_card_receipt
from services.pricing import PriceResult
from services.reseller import get_plan_price, set_plan_price
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    get_or_create_draft,
    set_category_flat_plan_list,
)
from services.store_strategy import upsert_draft_promo
from services.wallet import get_balance

ADMIN_ID = 4242
PRICE = 50_000


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        admin_telegram_ids=[ADMIN_ID],
        store_owner_telegram_ids=[ADMIN_ID],
        max_discount_percent=None,
        referral_referred_discount_percent=0,
        wallet_provision_fail_credit_max_n=1,
        default_panel_group_ids=[1],
        pasar_guard_base_url="https://panel.example.invalid",
        pasar_guard_api_key="test",
        pasar_guard_sub_path="/sub",
        pasar_guard_timeout=5,
    )


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


def _callback(data: str, *, telegram_id: int = ADMIN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=telegram_id),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_reply_markup=AsyncMock(), message_id=1),
    )


def _screen(monkeypatch: pytest.MonkeyPatch, module: object) -> SimpleNamespace:
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(module, "_tg_message", lambda _cb: msg)
    return msg


async def _admin(session: AsyncSession) -> User:
    admin = User(telegram_id=ADMIN_ID, referral_code="ADM4242", is_admin=True)
    session.add(admin)
    await session.flush()
    return admin


async def _card_order(session: AsyncSession) -> tuple[User, Order]:
    buyer = User(telegram_id=8001, referral_code="BUY8001")
    plan = Plan(
        name="card-review",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=PRICE,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add_all([buyer, plan])
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=buyer.id,
        plan=plan,
        price=PriceResult(
            original_price=PRICE,
            discount_amount=0,
            final_price=PRICE,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        ),
        service_name="ali",
    )
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=buyer.id,
        receipt_file_id="file-1",
        card_number_display="6037",
    )
    await session.commit()
    return buyer, order


class _RejectingPanel:
    async def get_user(self, username: str) -> UserResponse:
        _ = username
        raise PasarGuardNotFoundError("missing")

    async def create_user(self, payload: object) -> UserResponse:
        _ = payload
        raise PasarGuardAPIError("invalid group", status_code=422)

    async def aclose(self) -> None:
        return None


class _AcceptingPanel:
    async def get_user(self, username: str) -> UserResponse:
        _ = username
        raise PasarGuardNotFoundError("missing")

    async def create_user(self, payload: object) -> UserResponse:
        username = str(payload.username)
        return UserResponse(
            id=81,
            username=username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=f"https://panel.example.invalid/sub/{username}",
            data_limit=10 * 1024**3,
        )

    def subscription_url_from_response(self, panel_user: UserResponse) -> str:
        return str(panel_user.subscription_url)

    async def aclose(self) -> None:
        return None


def _patch_panel(monkeypatch: pytest.MonkeyPatch, panel: object) -> None:
    async def client(*_args: object, **_kwargs: object) -> object:
        return panel

    monkeypatch.setattr(admin_handlers, "get_pasarguard_client", client)
    monkeypatch.setattr(admin_handlers, "deliver_provisioned_service_card", AsyncMock())


@pytest.mark.asyncio
async def test_a_non_admin_cannot_approve_a_payment(session: AsyncSession) -> None:
    _buyer, order = await _card_order(session)
    stranger = User(telegram_id=1, referral_code="STR1", is_admin=False)
    session.add(stranger)
    await session.commit()
    callback = _callback(f"{PayCB.APPROVE_PREFIX}{order.id}", telegram_id=1)
    await admin_handlers.pay_approve(
        callback,  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(send_message=AsyncMock()),  # type: ignore[arg-type]
        stranger,
    )
    await session.refresh(order)
    payment = (await session.execute(select(Payment))).scalar_one()
    assert order.status == OrderStatus.PAYMENT_REVIEW
    assert payment.status == PaymentStatus.pending
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_a_failed_provision_keeps_the_payment_approval(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card was accepted. A dead panel must not put the order back in review."""
    _patch_panel(monkeypatch, _RejectingPanel())
    admin = await _admin(session)
    _buyer, order = await _card_order(session)
    await admin_handlers.pay_approve(
        _callback(f"{PayCB.APPROVE_PREFIX}{order.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(send_message=AsyncMock()),  # type: ignore[arg-type]
        admin,
    )
    await session.refresh(order)
    payment = (await session.execute(select(Payment))).scalar_one()
    assert order.status == OrderStatus.PROVISION_FAILED
    assert payment.status == PaymentStatus.verified


@pytest.mark.asyncio
async def test_approving_a_receipt_provisions_the_service(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_panel(monkeypatch, _AcceptingPanel())
    admin = await _admin(session)
    _buyer, order = await _card_order(session)
    await admin_handlers.pay_approve(
        _callback(f"{PayCB.APPROVE_PREFIX}{order.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(send_message=AsyncMock()),  # type: ignore[arg-type]
        admin,
    )
    await session.refresh(order)
    payment = (await session.execute(select(Payment))).scalar_one()
    assert order.status == OrderStatus.ACTIVE
    assert payment.status == PaymentStatus.verified


@pytest.mark.asyncio
async def test_wallet_adjust_credits_the_ledger_and_refuses_junk(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _screen(monkeypatch, admin_handlers)
    admin = await _admin(session)
    customer = User(telegram_id=8002, referral_code="BUY8002", first_name="علی")
    session.add(customer)
    await session.commit()
    state = _state()
    bot = SimpleNamespace(send_message=AsyncMock())
    await admin_handlers.admin_wallet_start(
        _callback(f"{AdminCB.WALLET_ADD}{customer.id}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    await admin_handlers.admin_wallet_amount(
        SimpleNamespace(  # type: ignore[arg-type]
            text="abc",
            from_user=SimpleNamespace(id=ADMIN_ID),
            message_id=3,
            answer=AsyncMock(),
        ),
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    assert await get_balance(session, customer.id) == 0
    assert (await state.get_data()).get("wallet_amount") is None

    await admin_handlers.admin_wallet_amount(
        SimpleNamespace(  # type: ignore[arg-type]
            text="۰",
            from_user=SimpleNamespace(id=ADMIN_ID),
            message_id=4,
            answer=AsyncMock(),
        ),
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    assert await get_balance(session, customer.id) == 0

    await admin_handlers.admin_wallet_amount(
        SimpleNamespace(  # type: ignore[arg-type]
            text="۵۰٬۰۰۰",
            from_user=SimpleNamespace(id=ADMIN_ID),
            message_id=5,
            answer=AsyncMock(),
        ),
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    await admin_handlers.admin_wallet_apply(
        _callback(f"{AdminCB.WALLET_OK}{customer.id}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
        bot,  # type: ignore[arg-type]
    )
    assert await get_balance(session, customer.id) == 50_000


@pytest.mark.asyncio
async def test_turning_a_promo_off_makes_checkout_refuse_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _screen(monkeypatch, store_admin)
    admin = await _admin(session)
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await upsert_draft_promo(session, code="OFF15", percent=15)
    await commit_and_activate(session, notes="کد تخفیف")
    buyer = User(telegram_id=8003, referral_code="BUY8003")
    session.add(buyer)
    await session.commit()
    live = await quote_checkout(
        session,
        _settings(),
        {
            "plan_type": "volume",
            "fixed_price_toman": 150_000,
            "volume_gb": 50,
            "duration_days": 30,
        },
        promo_code="OFF15",
        db_user=buyer,
    )
    assert live.final_price == 127_500

    await store_admin.store_promo_toggle(
        _callback(f"{StoreCB.PROMO_TG}OFF15"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        admin,
        session,
    )
    with pytest.raises(ValueError, match="invalid_promo_code"):
        await quote_checkout(
            session,
            _settings(),
            {
                "plan_type": "volume",
                "fixed_price_toman": 150_000,
                "volume_gb": 50,
                "duration_days": 30,
            },
            promo_code="OFF15",
            db_user=buyer,
        )


@pytest.mark.asyncio
async def test_percent_confirm_changes_only_the_open_duration_tab(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _screen(monkeypatch, admin_handlers)
    admin = await _admin(session)
    shop = User(telegram_id=8004, referral_code="SHOP8004", is_reseller=True)
    session.add(shop)
    await session.flush()
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    draft = await get_or_create_draft(session)
    cat_id = int(draft.categories[0].id)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    slug = (await get_catalog_view(session)).packages[0].slug
    await set_plan_price(session, shop.id, slug, 100_000, duration_days=30, tier="economy")
    await set_plan_price(session, shop.id, slug, 200_000, duration_days=60, tier="economy")
    await session.commit()

    state = _state()
    await state.update_data(
        rs_user_id=shop.id,
        rs_pct_value=10,
        rs_sel_duration_days=30,
        rs_sel_tier="economy",
        rs_pct_changes=[(slug, 100_000, 90_000)],
    )
    await admin_handlers.admin_reseller_pct_confirm(
        _callback(AdminCB.RS_PCT_OK),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    assert await get_plan_price(session, shop.id, slug, duration_days=30, tier="economy") == 90_000
    assert await get_plan_price(session, shop.id, slug, duration_days=60, tier="economy") == 200_000


async def _ladder(session: AsyncSession, prices: tuple[int, int]) -> int:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    draft = await get_or_create_draft(session)
    cat_id = int(draft.categories[0].id)
    small, big = prices
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۱۰ گیگ",
        fixed_price_toman=small,
        volume_gb=10,
        duration_days=30,
    )
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۲۰ گیگ",
        fixed_price_toman=big,
        volume_gb=20,
        duration_days=30,
    )
    await set_category_flat_plan_list(session, cat_id, enabled=True)
    await commit_and_activate(session, notes="نردبان")
    view = await get_catalog_view(session)
    return int(view.categories[0].id)


@pytest.mark.asyncio
async def test_the_dashboard_warns_when_a_bigger_plan_is_not_cheaper_per_gb(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = _screen(monkeypatch, store_admin)
    admin = await _admin(session)
    cat_id = await _ladder(session, (100_000, 300_000))
    await store_admin.store_cat_detail(
        _callback(f"{StoreCB.CAT}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        admin,
        session,
        _state(),
    )
    body = screen.edit_text.await_args.args[0]
    assert "نردبان قیمت" in body


@pytest.mark.asyncio
async def test_the_dashboard_stays_quiet_when_the_ladder_gets_cheaper(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = _screen(monkeypatch, store_admin)
    admin = await _admin(session)
    cat_id = await _ladder(session, (100_000, 160_000))
    await store_admin.store_cat_detail(
        _callback(f"{StoreCB.CAT}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        admin,
        session,
        _state(),
    )
    body = screen.edit_text.await_args.args[0]
    assert "نردبان قیمت" not in body
