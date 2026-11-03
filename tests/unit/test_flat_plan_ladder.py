"""Flat plan ladder («فهرست ساده») — multi-duration shelf without duration tabs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified

import database.models  # noqa: F401
from bot.config import Settings
from bot.handlers import purchase as purchase_handlers
from bot.handlers import store_admin as store_admin_handlers
from bot.keyboards.purchase import PurchaseCB, ready_plans_keyboard
from bot.keyboards.store_admin import StoreCB, store_cat_panel_keyboard, store_cat_sale_keyboard
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import User
from database.models.store import StorePackagePrice
from services.pricing import PlanType
from services.reseller import set_plan_price
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_or_create_draft,
    set_category_flat_plan_list,
    set_category_plan_order_mode,
    update_draft_package,
)
from services.store_catalog import (
    compact_toman_fa,
    flat_plan_button_label,
    flat_plan_list_enabled,
    ladder_warnings,
    normalize_category_sales_flags,
    plan_duration_label_fa,
)
from services.store_config import get_published_store_config
from services.store_pricing import DEFAULT_TIER, get_price
from services.ux_format import format_toman, to_persian_digits


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


def _cb(data: str, *, message: MagicMock | None = None) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=1)
    callback.answer = AsyncMock()
    callback.message = message
    return callback


def _plan_rows(markup) -> list:
    return [
        btn
        for row in markup.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith(PurchaseCB.READY_PREFIX)
    ]


async def _seed_flat_volume(session: AsyncSession, *, flat: bool = True) -> tuple:
    """10/30, 20/30, 50/60, 100/90 with descending per-GB prices."""
    cat = await create_draft_category(session, title_fa="حجمی نردبان", kind="volume")
    cat.sales_flags = {
        **dict(cat.sales_flags or {}),
        "durations": [30, 60, 90],
        "tiers_enabled": False,
        "flat_plan_list": flat,
    }
    flag_modified(cat, "sales_flags")
    specs = [
        ("۱۰ گیگ / ۱ ماه", 10, 30, 50_000),
        ("۲۰ گیگ / ۱ ماه", 20, 30, 90_000),
        ("۵۰ گیگ / ۲ ماه", 50, 60, 200_000),
        ("۱۰۰ گیگ / ۳ ماه", 100, 90, 350_000),
    ]
    for title, gb, days, price in specs:
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa=title,
            fixed_price_toman=price,
            volume_gb=gb,
            duration_days=days,
        )
    await commit_and_activate(session, notes="نردبان حجمی تست")
    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    return cfg, pub_cat


def test_normalize_flat_plan_list_defaults_false() -> None:
    flags = normalize_category_sales_flags({"kind": "volume"})
    assert flags["flat_plan_list"] is False


def test_plan_duration_label_fa_months_days_and_none() -> None:
    assert plan_duration_label_fa(duration_days=60) == f"{to_persian_digits(2)} ماهه"
    assert plan_duration_label_fa(duration_days=45) == f"{to_persian_digits(45)} روزه"
    assert plan_duration_label_fa(duration_days=None) == "بدون انقضا"
    assert plan_duration_label_fa(duration_days=0) == "بدون انقضا"
    assert plan_duration_label_fa(duration_months=3) == f"{to_persian_digits(3)} ماهه"


def test_compact_toman_fa() -> None:
    assert compact_toman_fa(430_000) == "۴۳۰ هزار"
    assert compact_toman_fa(1_250_000) == "۱٬۲۵۰ هزار"
    assert compact_toman_fa(12_500) == format_toman(12_500, persian_digits=True)


def test_flat_plan_button_label_volume_has_per_gb_no_toman() -> None:
    pkg = SimpleNamespace(
        title_fa="نادیده",
        options={"volume_gb": 100, "duration_days": 90},
    )
    label = flat_plan_button_label(pkg, 430_000)
    per_fa = format_toman(4_300, persian_digits=True)
    price_fa = compact_toman_fa(430_000)
    assert (
        label
        == f"{to_persian_digits(100)} گیگ \u2013 {to_persian_digits(3)} ماهه | {price_fa} (گیگی {per_fa})"
    )
    assert "گیگ – ۳ ماهه | ۴۳۰ هزار (گیگی ۴٬۳۰۰)" in label
    assert "هزار" in label
    assert "گیگی" in label
    assert "گیگی ۴٬۳۰۰" in label or f"گیگی {per_fa}" in label
    assert "تومان" not in label
    assert "—" not in label
    assert "·" not in label and "•" not in label
    assert " / " not in label
    assert "\u2013" in label
    assert "-" not in label
    assert label.count("|") == 1


def test_flat_plan_button_label_unlimited_no_per_gb() -> None:
    pkg = SimpleNamespace(
        title_fa="نادیده",
        options={"concurrent_users": 2, "duration_months": 3},
    )
    label = flat_plan_button_label(pkg, 450_000)
    price_fa = compact_toman_fa(450_000)
    assert label == f"{to_persian_digits(2)} کاربره \u2013 {to_persian_digits(3)} ماهه | {price_fa}"
    assert "هزار" in label
    assert "گیگی" not in label
    assert "تومان" not in label
    assert "·" not in label and "•" not in label
    assert " / " not in label
    assert "\u2013" in label
    assert "-" not in label
    assert label.count("|") == 1


def test_flat_plan_label_length_guard() -> None:
    """Telegram button text must stay short; ladder rows must stay distinct."""
    ladder = [
        (10, 30, 60_000),
        (20, 30, 115_000),
        (30, 30, 165_000),
        (50, 60, 255_000),
        (100, 90, 430_000),
        (200, 90, 700_000),
    ]
    labels = [
        flat_plan_button_label(
            SimpleNamespace(options={"volume_gb": gb, "duration_days": days}, slug="x"),
            price,
        )
        for gb, days, price in ladder
    ]
    assert all(len(label) <= 40 for label in labels)
    assert len(set(labels)) == len(labels)
    assert all("·" not in label and "•" not in label for label in labels)
    assert all(" / " not in label for label in labels)
    assert all("\u2013" in label and "-" not in label for label in labels)
    assert all(label.count("|") == 1 for label in labels)


def test_ladder_warnings_monotonic_empty() -> None:
    plans = [
        (SimpleNamespace(options={"volume_gb": 10, "duration_days": 30}), 50_000),
        (SimpleNamespace(options={"volume_gb": 20, "duration_days": 30}), 90_000),
        (SimpleNamespace(options={"volume_gb": 50, "duration_days": 60}), 200_000),
    ]
    assert ladder_warnings(plans) == []


def test_ladder_warnings_flags_non_monotonic_pair() -> None:
    """50 GB cheaper per GB than 100 GB → one warning naming both."""
    p50 = SimpleNamespace(options={"volume_gb": 50, "duration_days": 30}, title_fa="۵۰")
    p100 = SimpleNamespace(options={"volume_gb": 100, "duration_days": 30}, title_fa="۱۰۰")
    # 50→2000/GB, 100→2500/GB
    warnings = ladder_warnings([(p50, 100_000), (p100, 250_000)])
    assert len(warnings) == 1
    small, big, small_per, big_per = warnings[0]
    assert "۵۰" in small or to_persian_digits(50) in small
    assert "۱۰۰" in big or to_persian_digits(100) in big
    assert small_per == 2000
    assert big_per == 2500


@pytest.mark.asyncio
async def test_flat_shelf_four_plans_no_tabs_ordered(session: AsyncSession) -> None:
    cfg, cat = await _seed_flat_volume(session, flat=True)
    assert flat_plan_list_enabled(cat) is True
    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=cat, db_user=None
    )
    assert message.answer.await_count == 1
    body = message.answer.await_args.args[0]
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert not any((c or "").startswith(PurchaseCB.DUR_PREFIX) for c in cbs)
    assert not any((c or "").startswith(PurchaseCB.TIER_PREFIX) for c in cbs)
    plan_btns = _plan_rows(markup)
    assert len(plan_btns) == 4
    labels = [b.text for b in plan_btns]
    assert to_persian_digits(10) in labels[0] and "گیگ" in labels[0]
    assert to_persian_digits(20) in labels[1]
    assert f"{to_persian_digits(50)} گیگ \u2013 {to_persian_digits(2)} ماهه" in labels[2]
    assert f"{to_persian_digits(100)} گیگ \u2013 {to_persian_digits(3)} ماهه" in labels[3]
    # 50_000/10 = 5_000 → «گیگی ۵٬۰۰۰» on the button, not in the body
    assert f"گیگی {format_toman(5_000, persian_digits=True)}" in labels[0]
    assert f"گیگی {format_toman(3_500, persian_digits=True)}" in labels[3]
    assert all("تومان" not in lab for lab in labels)
    assert all("—" not in lab for lab in labels)
    assert all("·" not in lab and "•" not in lab for lab in labels)
    assert all(" / " not in lab for lab in labels)
    assert all(lab.count("|") == 1 for lab in labels)
    assert body == (f"{texts.purchase_flat_shelf_intro}\n{texts.purchase_flat_no_user_limit}")
    assert "•" not in body


@pytest.mark.asyncio
async def test_flat_off_still_shows_duration_tabs(session: AsyncSession) -> None:
    cfg, cat = await _seed_flat_volume(session, flat=False)
    assert flat_plan_list_enabled(cat) is False
    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=cat, db_user=None
    )
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any((c or "").startswith(PurchaseCB.DUR_PREFIX) for c in cbs)


@pytest.mark.asyncio
async def test_tapping_100gb_charges_own_90d_cell(session: AsyncSession) -> None:
    """Even if group's first duration tab is 30d, 100GB/90d pays its own cell."""
    cat = await create_draft_category(session, title_fa="حجمی نردبان", kind="volume")
    cat.sales_flags = {
        **dict(cat.sales_flags or {}),
        "durations": [30, 60, 90],
        "tiers_enabled": False,
        "flat_plan_list": True,
    }
    flag_modified(cat, "sales_flags")
    for title, gb, days, price in [
        ("۱۰", 10, 30, 50_000),
        ("۲۰", 20, 30, 90_000),
        ("۵۰", 50, 60, 200_000),
        ("۱۰۰", 100, 90, 350_000),
    ]:
        await create_draft_package(
            session,
            category_id=cat.id,
            title_fa=title,
            fixed_price_toman=price,
            volume_gb=gb,
            duration_days=days,
        )
    # Poison 30d cell on the 100GB plan — flat mode must charge 90d, not this.
    draft = await get_or_create_draft(session)
    pkg100_draft = next(p for p in draft.packages if (p.options or {}).get("volume_gb") == 100)
    session.add(
        StorePackagePrice(
            store_version_id=draft.id,
            package_slug=pkg100_draft.slug,
            duration_days=30,
            tier=DEFAULT_TIER,
            price_toman=999_999,
        )
    )
    await session.flush()
    await commit_and_activate(session, notes="نردبان با سلول سمّی ۳۰روزه")
    session.expire_all()
    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    pkg100 = next(p for p in cfg.packages if (p.options or {}).get("volume_gb") == 100)
    cell_90 = get_price(
        list(cfg.prices), slug=str(pkg100.slug), duration_days=90, tier=DEFAULT_TIER
    )
    cell_30 = get_price(
        list(cfg.prices), slug=str(pkg100.slug), duration_days=30, tier=DEFAULT_TIER
    )
    assert cell_90 == 350_000
    assert cell_30 == 999_999

    state = await _fsm()
    await state.set_state(PurchaseStates.choosing_ready_plan)
    await state.update_data(
        store_category_id=pub_cat.id,
        plan_type=PlanType.VOLUME.value,
        sel_duration_days=30,
        sel_tier=DEFAULT_TIER,
    )
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    asked: dict = {}

    async def _capture_ask(msg, st, db_user=None, session=None, settings=None):
        asked["data"] = await st.get_data()

    original = purchase_handlers._ask_service_name
    purchase_handlers._ask_service_name = _capture_ask  # type: ignore[assignment]
    try:
        callback = _cb(f"{PurchaseCB.READY_PREFIX}{pkg100.id}", message=message)
        await purchase_handlers.choose_ready_plan(
            callback, state, session, _settings(), db_user=None
        )
    finally:
        purchase_handlers._ask_service_name = original  # type: ignore[assignment]

    assert asked["data"]["fixed_price_toman"] == 350_000
    assert asked["data"]["fixed_price_toman"] != 999_999
    assert asked["data"]["duration_days"] == 90
    assert asked["data"]["volume_gb"] == 100


@pytest.mark.asyncio
async def test_manual_order_respected_in_flat_mode(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی دستی", kind="volume")
    cat.sales_flags = {
        **dict(cat.sales_flags or {}),
        "durations": [30, 60],
        "flat_plan_list": True,
    }
    flag_modified(cat, "sales_flags")
    small = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰",
        fixed_price_toman=40_000,
        volume_gb=10,
        duration_days=30,
    )
    big = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=60,
    )
    await set_category_plan_order_mode(session, cat.id, mode="manual")
    # Put the bigger plan first in manual sort_order.
    await update_draft_package(session, big.id, sort_order=10)
    await update_draft_package(session, small.id, sort_order=20)
    await commit_and_activate(session, notes="ترتیب دستی تخت")
    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=pub_cat, db_user=None
    )
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    labels = [b.text for b in _plan_rows(markup)]
    assert to_persian_digits(50) in labels[0]
    assert to_persian_digits(10) in labels[1]


@pytest.mark.asyncio
async def test_unlimited_flat_labels_no_per_gb(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="نامحدود تخت", kind="unlimited")
    cat.sales_flags = {**dict(cat.sales_flags or {}), "flat_plan_list": True}
    flag_modified(cat, "sales_flags")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۲ کاربره / ۳ ماه",
        fixed_price_toman=450_000,
        concurrent_users=2,
        duration_months=3,
    )
    await commit_and_activate(session, notes="نامحدود تخت")
    cfg = await get_published_store_config(session)
    assert cfg is not None
    pub_cat = next(c for c in cfg.categories if c.slug == cat.slug)
    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=pub_cat, db_user=None
    )
    body = message.answer.await_args.args[0]
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    labels = [b.text for b in _plan_rows(markup)]
    assert any(
        f"{to_persian_digits(2)} کاربره \u2013 {to_persian_digits(3)} ماهه | "
        f"{compact_toman_fa(450_000)}" == lab
        for lab in labels
    )
    assert all("گیگی" not in lab for lab in labels)
    assert all("تومان" not in lab for lab in labels)
    assert all("·" not in lab and "•" not in lab for lab in labels)
    assert all(" / " not in lab for lab in labels)
    assert all(lab.count("|") == 1 for lab in labels)
    assert body == texts.purchase_flat_shelf_intro_unlimited
    assert "•" not in body


@pytest.mark.asyncio
async def test_reseller_sees_own_price_at_plan_duration(session: AsyncSession) -> None:
    cfg, cat = await _seed_flat_volume(session, flat=True)
    shop = User(telegram_id=900001, referral_code="SHOPFLAT1", is_reseller=True)
    session.add(shop)
    await session.commit()
    await session.refresh(shop)
    pkg100 = next(p for p in cfg.packages if (p.options or {}).get("volume_gb") == 100)
    pkg10 = next(p for p in cfg.packages if (p.options or {}).get("volume_gb") == 10)
    await set_plan_price(
        session, shop.id, str(pkg100.slug), 300_000, duration_days=90, tier=DEFAULT_TIER
    )
    await set_plan_price(
        session, shop.id, str(pkg10.slug), 40_000, duration_days=30, tier=DEFAULT_TIER
    )
    await session.commit()

    state = await _fsm()
    message = MagicMock()
    message.answer = AsyncMock()
    await purchase_handlers._after_category_entry(
        message, state, session, _settings(), cat=cat, db_user=shop
    )
    markup = (
        message.answer.await_args.kwargs.get("reply_markup") or message.answer.await_args.args[1]
    )
    labels = [b.text for b in _plan_rows(markup)]
    joined = "\n".join(labels)
    assert compact_toman_fa(300_000) in joined
    assert compact_toman_fa(40_000) in joined
    # Retail 350k must not appear for the 100GB plan when reseller price exists.
    assert compact_toman_fa(350_000) not in joined
    assert format_toman(350_000, persian_digits=True) not in joined


def test_settings_keyboard_flat_hides_durs_and_tiers() -> None:
    off = SimpleNamespace(
        id=5,
        enabled=True,
        sales_flags={
            "kind": "volume",
            "allow_new_sale": True,
            "allow_renew": True,
            "flat_plan_list": False,
        },
    )
    kb_off = store_cat_sale_keyboard(off)
    cbs_off = [btn.callback_data for row in kb_off.inline_keyboard for btn in row]
    assert f"{StoreCB.CAT_FLAT}{off.id}" in cbs_off
    assert f"{StoreCB.CAT_DURS}{off.id}" in cbs_off
    assert f"{StoreCB.CAT_TIERS}{off.id}" in cbs_off
    assert not any((c or "").startswith(StoreCB.CAT_TL) for c in cbs_off)

    on = SimpleNamespace(
        id=5,
        enabled=True,
        sales_flags={
            "kind": "volume",
            "allow_new_sale": True,
            "allow_renew": True,
            "flat_plan_list": True,
            "tiers_enabled": True,
        },
    )
    kb_on = store_cat_sale_keyboard(on)
    cbs_on = [btn.callback_data for row in kb_on.inline_keyboard for btn in row]
    assert f"{StoreCB.CAT_FLAT}{on.id}" in cbs_on
    assert f"{StoreCB.CAT_DURS}{on.id}" not in cbs_on
    assert f"{StoreCB.CAT_TIERS}{on.id}" not in cbs_on
    # The premium panel group is hidden along with the tier switch: under the
    # flat list nothing reads it.
    cbs_panel = [
        btn.callback_data for row in store_cat_panel_keyboard(on).inline_keyboard for btn in row
    ]
    assert f"{StoreCB.CAT_TIER_GROUPS}{on.id}" not in cbs_panel
    assert not any((c or "").startswith(StoreCB.CAT_TL) for c in cbs_on)


@pytest.mark.asyncio
async def test_cat_tl_answers_retired_alert() -> None:
    cb = _cb(f"{StoreCB.CAT_TL}9")
    await store_admin_handlers.store_cat_toggle_tl(cb)
    assert cb.answer.await_count == 1
    assert texts.admin_store_tl_filter_retired in cb.answer.await_args.args[0]
    assert cb.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_set_category_flat_plan_list_persists(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="گروه", kind="volume")
    await set_category_flat_plan_list(session, cat.id, enabled=True)
    await session.refresh(cat)
    assert flat_plan_list_enabled(cat) is True


def test_flat_keyboard_helper_omits_tabs() -> None:
    pkg = SimpleNamespace(
        id=1,
        slug="a",
        title_fa="باید نادیده بشه",
        options={"volume_gb": 50, "duration_days": 60},
    )
    markup = ready_plans_keyboard(
        [pkg],
        include_custom=False,
        prices_by_slug={"a": 200_000},
        durations=[30, 60],
        tiers=[DEFAULT_TIER, "premium"],
        selected_duration=30,
        selected_tier=DEFAULT_TIER,
        flat_mode=True,
    )
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert not any((c or "").startswith(PurchaseCB.DUR_PREFIX) for c in cbs)
    assert not any((c or "").startswith(PurchaseCB.TIER_PREFIX) for c in cbs)
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    price_fa = compact_toman_fa(200_000)
    per_fa = format_toman(4_000, persian_digits=True)
    expected = f"{to_persian_digits(50)} گیگ \u2013 {to_persian_digits(2)} ماهه | {price_fa} (گیگی {per_fa})"
    assert expected in labels
    assert all("تومان" not in t for t in labels if t)
    plan_labels = [t for t in labels if t and "گیگ" in t]
    assert all("·" not in t and "•" not in t for t in plan_labels)
    assert all(" / " not in t for t in plan_labels)
    assert all(t.count("|") == 1 for t in plan_labels)
