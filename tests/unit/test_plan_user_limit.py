"""T-035: per-plan / per-category volume hwid_limit is real and reaches the panel."""

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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from bot.config import Settings
from bot.handlers import purchase as purchase_handlers
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import User
from database.models.store import StoreVersion
from services.orders import create_pending_order, get_or_create_dynamic_plan
from services.payments import approve_payment, submit_card_receipt
from services.pricing import PlanType, PriceResult
from services.provisioning import provision_order
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    set_category_flat_plan_list,
    set_category_volume_hwid_limit,
    set_package_hwid_limit,
)
from services.store_catalog import (
    category_volume_hwid_limit,
    package_hwid_limit,
    ready_package_purchase_payload,
)
from services.store_config import get_published_store_config
from services.ux_format import to_persian_digits


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        PASARGUARD_BASE_URL="https://panel.example.com",
        PASARGUARD_API_KEY="pg_key_testvvvv-test-4est-8est-testtesttest",
        PASARGUARD_SUB_PATH="sub",
        PASARGUARD_TIMEOUT=10.0,
        DEFAULT_PANEL_GROUP_IDS=[900],
        ADMIN_TELEGRAM_IDS=[1],
        STORE_OWNER_TELEGRAM_IDS=[1],
    )


async def _fsm() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage, key=key)


def _callback(data: str) -> SimpleNamespace:
    msg = SimpleNamespace(
        answer=AsyncMock(),
        edit_text=AsyncMock(),
        chat=SimpleNamespace(id=1),
        message_id=1,
    )
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=msg,
        from_user=SimpleNamespace(id=1),
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1),
    )


def _panel_user(username: str) -> UserResponse:
    return UserResponse(
        id=42,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=10 * 1024**3,
    )


async def _provision_volume_order(session: AsyncSession, *, hwid_limit: int) -> object:
    """Create → approve → provision a volume order; return UserCreate payload."""
    customer = User(
        telegram_id=1001,
        username="cust",
        first_name="C",
        last_name=None,
        language_code="fa",
        referral_code="CUST0001",
        is_admin=False,
    )
    admin = User(
        telegram_id=9001,
        username="admin",
        first_name="A",
        last_name=None,
        language_code="fa",
        referral_code="ADMIN001",
        is_admin=True,
    )
    session.add_all([customer, admin])
    await session.flush()
    price = PriceResult(
        original_price=100_000,
        discount_amount=0,
        final_price=100_000,
        currency="IRT",
        meta={"volume_gb": 10, "duration_days": 30},
    )
    plan = await get_or_create_dynamic_plan(
        session,
        plan_type=PlanType.VOLUME,
        price=price,
        group_ids=[1],
        hwid_limit=hwid_limit,
    )
    order = await create_pending_order(
        session, user_id=customer.id, plan=plan, price=price, service_name="reza"
    )
    await session.flush()
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=customer.id,
        receipt_file_id="file",
        card_number_display="6037",
    )
    await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url
    await provision_order(session, client, order_id=order.id, default_group_ids=[9])
    return client.create_user.await_args.args[0]


@pytest.mark.asyncio
async def test_ready_volume_hwid_zero_reaches_user_create(session: AsyncSession) -> None:
    hwid = await purchase_handlers._resolve_volume_hwid_limit(
        session, {"hwid_limit": 0, "plan_type": PlanType.VOLUME.value}
    )
    assert hwid == 0
    payload = await _provision_volume_order(session, hwid_limit=hwid)
    assert payload.hwid_limit == 0


@pytest.mark.asyncio
async def test_ready_volume_hwid_three_reaches_user_create(session: AsyncSession) -> None:
    payload = await _provision_volume_order(session, hwid_limit=3)
    assert payload.hwid_limit == 3


@pytest.mark.asyncio
async def test_missing_hwid_key_defaults_to_zero(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    pkg = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰ گیگ",
        fixed_price_toman=90_000,
        volume_gb=10,
        duration_days=30,
        options={},  # no hwid_limit key — create_draft_package still defaults to 0
    )
    # Force-missing key the way a legacy row looks.
    pkg.options = {"volume_gb": 10, "duration_days": 30}
    await session.flush()
    assert package_hwid_limit(pkg) == 0
    payload = ready_package_purchase_payload(pkg, [])
    assert payload["hwid_limit"] == 0


@pytest.mark.asyncio
async def test_custom_volume_uses_category_hwid(session: AsyncSession) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    live = view.categories[0]
    assert category_volume_hwid_limit(live) == 0

    await set_category_volume_hwid_limit(session, live.id, hwid_limit=2)
    await commit_and_activate(session, notes="کاربر همزمان خرید سفارشی عوض شد")
    view = await get_catalog_view(session)
    live = view.categories[0]
    assert category_volume_hwid_limit(live) == 2

    data = {"store_category_id": live.id, "plan_type": PlanType.VOLUME.value}
    assert await purchase_handlers._resolve_volume_hwid_limit(session, data) == 2


@pytest.mark.asyncio
async def test_or_one_fallback_would_break_zero(session: AsyncSession) -> None:
    """Mutation check: ``0 or 1`` must not be how volume hwid is resolved."""
    data = {"hwid_limit": 0, "plan_type": PlanType.VOLUME.value}
    resolved = await purchase_handlers._resolve_volume_hwid_limit(session, data)
    assert resolved == 0
    # The classic bug:
    assert (data.get("hwid_limit") or 1) == 1
    assert resolved != (data.get("hwid_limit") or 1)


@pytest.mark.asyncio
async def test_wizard_nameless_users_persists_zero(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id
    state = await _fsm()
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    monkeypatch.setattr(ha, "_edit_or_answer", AsyncMock())

    await ha.store_pkg_add_start(
        _callback(f"{StoreCB.PKG_ADD}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}50"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}30"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    # Nothing written before confirm.
    assert (await session.scalar(select(func.count()).select_from(StoreVersion))) == 1
    await ha.store_pkg_users_pick(
        _callback(f"{StoreCB.PKG_USERS}0"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_add_price(
        _typed("150000"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    data = await state.get_data()
    assert data.get("new_pkg_hwid") == 0
    assert "کاربر همزمان: نامحدود" in ha._pending_plan_preview_text(data)

    await ha.store_pkg_activate(
        _callback(StoreCB.PKG_ACTIVATE),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    view = await get_catalog_view(session)
    pkg = view.packages[0]
    assert package_hwid_limit(pkg) == 0


@pytest.mark.asyncio
async def test_wizard_typed_two_and_rejects_garbage(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id
    state = await _fsm()
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    monkeypatch.setattr(ha, "_edit_or_answer", AsyncMock())

    await ha.store_pkg_add_start(
        _callback(f"{StoreCB.PKG_ADD}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}50"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}30"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    msg = _typed("abc")
    await ha.store_pkg_users_typed(
        msg,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    msg.answer.assert_awaited()
    assert texts.admin_store_invalid_input in msg.answer.await_args.args[0]

    msg_neg = _typed("-1")
    await ha.store_pkg_users_typed(
        msg_neg,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    assert texts.admin_store_invalid_input in msg_neg.answer.await_args.args[0]

    await ha.store_pkg_users_typed(
        _typed("۲"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    await ha.store_pkg_add_price(
        _typed("150000"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    await ha.store_pkg_activate(
        _callback(StoreCB.PKG_ACTIVATE),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    view = await get_catalog_view(session)
    assert package_hwid_limit(view.packages[0]) == 2


@pytest.mark.asyncio
async def test_edit_plan_and_category_hwid_publish(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
        hwid_limit=0,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    before = await session.scalar(select(func.count()).select_from(StoreVersion))
    view = await get_catalog_view(session)
    pkg_id = view.packages[0].id
    cat_id = view.categories[0].id

    state = await _fsm()
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    monkeypatch.setattr(ha, "_edit_or_answer", AsyncMock())
    monkeypatch.setattr(ha, "_render_section", AsyncMock())

    await ha.store_pkg_hwid_start(
        _callback(f"{StoreCB.PKG_HWID}{pkg_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_users_pick(
        _callback(f"{StoreCB.PKG_USERS}3"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    after_pkg = await session.scalar(select(func.count()).select_from(StoreVersion))
    assert after_pkg == before + 1
    view = await get_catalog_view(session)
    assert package_hwid_limit(view.packages[0]) == 3

    await ha.store_cat_hwid_start(
        _callback(f"{StoreCB.CAT_HWID}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_users_pick(
        _callback(f"{StoreCB.PKG_USERS}2"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    after_cat = await session.scalar(select(func.count()).select_from(StoreVersion))
    assert after_cat == after_pkg + 1
    view = await get_catalog_view(session)
    assert category_volume_hwid_limit(view.categories[0]) == 2

    # Notes were Persian and committed — newest versions carry them.
    versions = (
        (await session.execute(select(StoreVersion).order_by(StoreVersion.version.desc())))
        .scalars()
        .all()
    )
    assert versions[0].notes == "کاربر همزمان خرید سفارشی عوض شد"
    assert versions[1].notes == "کاربر همزمان پلن عوض شد"


@pytest.mark.asyncio
async def test_flat_shelf_intro_only_when_all_unlimited(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۳۰ گیگ",
        fixed_price_toman=90_000,
        volume_gb=30,
        duration_days=30,
        hwid_limit=0,
    )
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=120_000,
        volume_gb=50,
        duration_days=30,
        hwid_limit=0,
    )
    await set_category_flat_plan_list(session, cat.id, enabled=True)
    await commit_and_activate(session, notes="فهرست ساده")
    cfg = await get_published_store_config(session)
    assert cfg is not None
    live_cat = cfg.categories[0]

    answers: list[str] = []

    async def _capture(*args, **kwargs):  # noqa: ANN001
        answers.append(args[0] if args else kwargs.get("text") or kwargs.get("body"))

    message = SimpleNamespace(answer=AsyncMock(side_effect=_capture), edit_text=AsyncMock())
    state = await _fsm()
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await purchase_handlers._render_ready_shelf(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        cat=live_cat,
        edit=False,
    )
    assert texts.purchase_flat_no_user_limit in answers[0]

    # One limited plan → claim disappears.
    view = await get_catalog_view(session)
    limited = next(p for p in view.packages if package_hwid_limit(p) == 0)
    await set_package_hwid_limit(session, limited.id, hwid_limit=2)
    await commit_and_activate(session, notes="کاربر همزمان پلن عوض شد")
    cfg = await get_published_store_config(session)
    live_cat = cfg.categories[0]
    answers.clear()
    await purchase_handlers._render_ready_shelf(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        cat=live_cat,
        edit=False,
    )
    assert texts.purchase_flat_no_user_limit not in answers[0]


@pytest.mark.asyncio
async def test_summary_shows_user_limit_line() -> None:
    zero = purchase_handlers._summary_details(
        {
            "plan_type": PlanType.VOLUME.value,
            "volume_gb": 50,
            "duration_days": 30,
            "hwid_limit": 0,
        }
    )
    assert "کاربر همزمان: نامحدود" in zero
    two = purchase_handlers._summary_details(
        {
            "plan_type": PlanType.VOLUME.value,
            "volume_gb": 50,
            "duration_days": 30,
            "hwid_limit": 2,
        }
    )
    assert f"کاربر همزمان: {to_persian_digits(2)}" in two


@pytest.mark.asyncio
async def test_unlimited_plan_still_uses_concurrent_users(session: AsyncSession) -> None:
    price = PriceResult(
        original_price=200_000,
        discount_amount=0,
        final_price=200_000,
        currency="IRT",
        meta={"concurrent_users": 4, "duration_months": 1},
    )
    plan = await get_or_create_dynamic_plan(
        session,
        plan_type=PlanType.UNLIMITED,
        price=price,
        group_ids=[1],
        hwid_limit=99,  # must be overwritten by concurrent_users
    )
    assert plan.hwid_limit == 4
