"""The store group is the ONLY place a service's panel groups come from.

Three sources used to compete (the group's own ids, a product-type map, a
default). The owner set the map and nothing changed, because the group still
held the [1, 2] the seed had copied onto it. Now a group with no panel group
resolves to ``[]`` and is not for sale: hidden from the menu and the trial
offers, refused at checkout, and refused in provisioning before any panel call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
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
from bot.config import Settings
from bot.handlers import admin as admin_handlers
from bot.handlers import purchase
from bot.handlers import store_admin as store_handlers
from bot.keyboards.admin import panel_settings_keyboard
from bot.texts import texts
from database.base import Base
from database.models import Order, User
from services.panel_settings import (
    _ensure_row,
    resolve_groups_for_plan_type,
)
from services.pricing import PlanType
from services.provisioning import ProvisionError, provision_order
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    update_draft_category,
)
from services.trial import list_trial_offers
from tests.unit.test_provisioning import _approved_order_with_groups

SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    DEFAULT_PANEL_GROUP_IDS=[900],
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


async def _plant_every_old_fallback(session: AsyncSession) -> None:
    """The product map and the DB default, as the old owner screen wrote them."""
    row = await _ensure_row(session)
    row.product_group_ids = {"volume": [500], "unlimited": [501]}
    row.default_group_ids = [700]
    await session.flush()


async def _sellable_group(session: AsyncSession, title: str, groups: str | None) -> object:
    cat = await create_draft_category(session, title_fa=title, kind="volume")
    if groups is not None:
        await update_draft_category(session, cat.id, panel_group_ids=groups)
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="10 گیگ / 30 روز",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    return cat


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolution_ignores_product_map_db_default_and_env_default(
    session: AsyncSession,
) -> None:
    await _plant_every_old_fallback(session)
    bare = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await session.commit()
    for plan_type in (PlanType.VOLUME, PlanType.UNLIMITED):
        assert (
            await resolve_groups_for_plan_type(
                session, SETTINGS, plan_type, store_category_id=bare.id
            )
            == []
        )
        assert await resolve_groups_for_plan_type(session, SETTINGS, plan_type) == []


@pytest.mark.asyncio
async def test_a_child_inherits_its_parent_and_a_bare_pair_resolves_to_nothing(
    session: AsyncSession,
) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await session.commit()
    args = (session, SETTINGS, PlanType.VOLUME)
    assert await resolve_groups_for_plan_type(*args, store_category_id=child.id) == []
    await update_draft_category(session, parent.id, panel_group_ids="7,8")
    await session.commit()
    assert await resolve_groups_for_plan_type(*args, store_category_id=child.id) == [7, 8]


# --------------------------------------------------------------------------- #
# A group with no panel group is not for sale
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_group_without_panel_groups_is_hidden_from_the_menu(
    session: AsyncSession,
) -> None:
    await _plant_every_old_fallback(session)
    await _sellable_group(session, "دارای گروه", "11")
    await _sellable_group(session, "بدون گروه", None)
    # A parent whose only child has none disappears with it; a child that
    # inherits keeps its parent on the menu.
    hollow_parent = await create_draft_category(session, title_fa="والد خالی", kind="volume")
    hollow = await create_draft_category(
        session, title_fa="فرزند خالی", kind="volume", parent_id=hollow_parent.id
    )
    await create_draft_package(
        session,
        category_id=hollow.id,
        title_fa="10 گیگ / 30 روز",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    full_parent = await create_draft_category(session, title_fa="والد پر", kind="volume")
    await update_draft_category(session, full_parent.id, panel_group_ids="5")
    inheriting = await create_draft_category(
        session, title_fa="فرزند", kind="volume", parent_id=full_parent.id
    )
    await create_draft_package(
        session,
        category_id=inheriting.id,
        title_fa="10 گیگ / 30 روز",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    await commit_and_activate(session, notes="seed")

    shown = {c.title_fa for c in await purchase._published_sale_categories(session)}
    assert shown == {"دارای گروه", "والد پر"}


@pytest.mark.asyncio
async def test_a_group_without_panel_groups_offers_no_trial(session: AsyncSession) -> None:
    from services.store_admin import set_category_trial

    await _plant_every_old_fallback(session)
    with_groups = await create_draft_category(session, title_fa="الف", kind="volume")
    await update_draft_category(session, with_groups.id, panel_group_ids="3")
    without = await create_draft_category(session, title_fa="ب", kind="volume")
    for cat in (with_groups, without):
        await set_category_trial(session, cat.id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="seed")

    offers = await list_trial_offers(session, SETTINGS)
    assert [o.slug for o in offers] == [str(with_groups.slug)]
    assert offers[0].group_ids == [3]


# --------------------------------------------------------------------------- #
# Checkout and provisioning refuse
# --------------------------------------------------------------------------- #


class _Message:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def answer(self, text: str, reply_markup: object = None) -> None:
        _ = reply_markup
        self.texts.append(text)


@pytest.mark.asyncio
async def test_checkout_with_no_groups_creates_no_order(session: AsyncSession, tmp_path) -> None:
    import json

    doc = {
        "volume_plan": {
            "tiers": [{"max_gb": None, "price_per_gb_toman": "1000"}],
            "duration_discounts": [],
        },
        "unlimited_plan": {
            "base_price_first_user_toman": "1000",
            "additional_user_coefficients": ["1"],
            "duration_discounts": [],
        },
        "promo_codes": {},
        "sales_rules": {"discount_combine": "best_of_promo_vs_extra"},
    }
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    settings = SimpleNamespace(
        max_discount_percent=None,
        referral_referred_discount_percent=0,
        pricing_config_path=str(path),
        wallet_provision_fail_credit_max_n=1,
    )
    user = User(telegram_id=1001, referral_code="C1001", wallet_balance_toman=0)
    session.add(user)
    await session.commit()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.update_data(
        plan_type="volume",
        volume_gb=10,
        duration_days=30,
        fixed_price_toman=50_000,
        hwid_limit=0,
        service_name="ali",
        sel_duration_days=30,
        wallet_applied=0,
    )
    message = _Message()
    await purchase.confirm_order(
        SimpleNamespace(answer=AsyncMock(), message=message),  # type: ignore[arg-type]
        state,
        session,
        settings,  # type: ignore[arg-type]
        user,
    )
    assert message.texts == [texts.purchase_group_not_ready]
    assert (await session.execute(select(Order))).scalars().all() == []


@pytest.mark.asyncio
async def test_provision_create_with_no_groups_never_reaches_the_panel(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _customer, order = await _approved_order_with_groups(session, [])
    client = AsyncMock()
    with pytest.raises(ProvisionError, match="no_panel_group"):
        await provision_order(session, client, order_id=order.id)
    assert client.mock_calls == []
    await session.refresh(order)
    assert order.panel_username is None


# --------------------------------------------------------------------------- #
# Admin screens
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_panel_settings_screen_warns_on_an_unset_group_and_has_no_map(
    session: AsyncSession,
) -> None:
    await _plant_every_old_fallback(session)
    await _sellable_group(session, "دارای گروه", "11,12")
    await _sellable_group(session, "بدون گروه", None)
    await commit_and_activate(session, notes="seed")

    text = await admin_handlers._format_panel_settings(session, SETTINGS)
    assert "دارای گروه: 11, 12" in text
    assert "بدون گروه: ⚠️ تنظیم نشده — فروخته نمی‌شود" in text
    assert "برای تغییر: فروشگاه ← گروه ← ⚙️ تنظیمات بیشتر ← گروه پنل" in text
    assert "پیش‌فرض" not in text
    assert "500" not in text and "700" not in text and "900" not in text

    callbacks = [
        btn.callback_data or "" for row in panel_settings_keyboard().inline_keyboard for btn in row
    ]
    labels = [btn.text for row in panel_settings_keyboard().inline_keyboard for btn in row]
    assert not any(c.startswith(("adm:ppg:", "adm:ppt:", "adm:pps:", "adm:ppc")) for c in callbacks)
    assert not any("group_ids" in c for c in callbacks)
    assert not any("گروه‌های پیش‌فرض" in t or "گروه پلن" in t for t in labels)
    assert any("لیست گروه‌های پنل" in t for t in labels)


@pytest.mark.asyncio
async def test_the_store_card_says_where_the_group_comes_from(session: AsyncSession) -> None:
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    own = await create_draft_category(session, title_fa="اقتصادی", kind="volume")
    await update_draft_category(session, own.id, panel_group_ids="4")
    await commit_and_activate(session, notes="seed")

    from services.store_admin import get_catalog_view

    view = await get_catalog_view(session)
    live = {c.title_fa: c for c in view.categories}

    def card(title: str) -> str:
        cat = live[title]
        return store_handlers._dashboard_text(view, cat)

    assert texts.admin_store_cat_groups_unset in card("ویژه")
    assert "⚠️ تنظیم نشده؛ این گروه فروخته نمی‌شود" in card("حجمی")
    assert "گروه پنل: 4" in card("اقتصادی")
    # The same warning is counted on the hub, once per group nobody can buy from.
    hub_text, _hub_groups = store_handlers._hub_summary(view, is_owner=True)
    assert "⚠️" in hub_text

    await update_draft_category(session, live["حجمی"].id, panel_group_ids="7,8")
    await commit_and_activate(session, notes="parent groups")
    view = await get_catalog_view(session)
    live = {c.title_fa: c for c in view.categories}
    assert "از گروه والد: 7، 8" in card("ویژه")
    _ = child


@pytest.mark.asyncio
async def test_a_plan_switch_with_no_groups_never_reaches_the_panel(
    session: AsyncSession,
) -> None:
    from tests.unit.test_plan_switch import _approved_switch, _Panel, _tunnel_plan
    from tests.unit.test_renew_same_plan import _bought, _catalog

    germany, _ = await _catalog(session)
    user, account = await _bought(session, germany=germany, slug="de-30", group_ids=[11])
    plan = await _tunnel_plan(session, gb=100, days=90, hwid=0)
    plan.group_ids = []
    order = await _approved_switch(session, user, account, plan)
    panel = _Panel(expire_in_days=25, data_limit=0, used=40 * 1024**3, hwid=2)

    with pytest.raises(ProvisionError, match="no_panel_group"):
        await provision_order(session, panel.client, order_id=order.id)
    assert panel.calls == []
