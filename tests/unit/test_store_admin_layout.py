"""The store admin layout: a dashboard per group, four sections, hide-until-relevant.

What these pin is *where a control lives and when it shows*, never what it does
(the toggles' effects are covered by their own tests). Each hide rule is checked
both ways — absent when irrelevant, present when relevant — so an
"always visible" mutation of the real line turns a test red.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import store_admin as ha
from bot.keyboards import style
from bot.keyboards.store_admin import (
    StoreCB,
    plan_button_label,
    store_cat_group_keyboard,
    store_cat_panel_keyboard,
    store_cat_sale_keyboard,
    store_cat_trial_keyboard,
    store_category_dashboard_keyboard,
    store_hub_keyboard,
    store_package_detail_keyboard,
    store_plan_order_keyboard,
)
from database.base import Base
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    set_category_trial,
    update_draft_category,
)

OWNER_ID = 4242


def _labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _cbs(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


def _has(markup: InlineKeyboardMarkup, prefix: str) -> bool:
    return any(c.startswith(prefix) for c in _cbs(markup))


def _cat(cid: int = 5, *, parent_id: int | None = None, **flags: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"kind": "volume", "allow_new_sale": True, "allow_renew": True}
    base.update(flags)
    return SimpleNamespace(id=cid, enabled=True, parent_id=parent_id, sales_flags=base)


def _pkg(
    *,
    volume_gb: int | None = 100,
    duration_days: int | None = 90,
    users: int | None = None,
    months: int | None = None,
    enabled: bool = True,
    allow_new_sale: bool = True,
    kind: str = "ready",
) -> SimpleNamespace:
    options: dict[str, Any] = {}
    if users is not None:
        options = {"concurrent_users": users, "duration_months": months}
    elif volume_gb is not None:
        options = {"volume_gb": volume_gb, "duration_days": duration_days}
    return SimpleNamespace(
        id=3,
        title_fa="عنوان",
        kind=SimpleNamespace(value=kind),
        options=options,
        enabled=enabled,
        allow_new_sale=allow_new_sale,
        allow_renew=True,
        forbid_extra_discount=False,
        category_id=5,
        sales_flags={},
    )


# --------------------------------------------------------------------------- #
# Plan buttons on the dashboard
# --------------------------------------------------------------------------- #


def test_plan_button_is_dims_plus_price_in_the_shelf_form() -> None:
    label = plan_button_label(_pkg(), 430_000)
    assert label == "۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار"
    assert "تومان" not in label
    assert "–" in label  # en dash, not a hyphen beside a number


def test_plan_button_marks_a_plan_that_is_off_sale() -> None:
    assert plan_button_label(_pkg(allow_new_sale=False), 430_000).startswith("⏸ ")
    assert plan_button_label(_pkg(enabled=False), 430_000).startswith("⏸ ")
    assert not plan_button_label(_pkg(), 430_000).startswith("⏸")


def test_unlimited_plan_button_shows_users_months_and_price() -> None:
    label = plan_button_label(_pkg(users=2, months=3), 250_000)
    assert label == "۲ کاربره – ۳ ماهه | ۲۵۰ هزار"


def test_plan_button_never_exceeds_forty_characters() -> None:
    label = plan_button_label(
        _pkg(volume_gb=100_000, duration_days=45, allow_new_sale=False), 1_234_567_890
    )
    assert len(label) <= 40


def test_dashboard_lists_the_plans_as_buttons_with_their_price_and_state() -> None:
    cat = _cat()
    kb = store_category_dashboard_keyboard(
        cat,
        is_owner=True,
        plan_buttons=[(11, "۵۰ گیگ – ۱ ماهه | ۹۰ هزار"), (12, "⏸ ۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار")],
    )
    labels = _labels(kb)
    assert "۵۰ گیگ – ۱ ماهه | ۹۰ هزار" in labels
    assert "⏸ ۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار" in labels
    assert f"{StoreCB.PKG}11" in _cbs(kb) and f"{StoreCB.PKG}12" in _cbs(kb)
    # No detour through «پلن‌های این گروه».
    assert not _has(kb, StoreCB.PKGS)


def test_dashboard_offers_the_four_sections_and_ends_on_a_lone_back() -> None:
    kb = store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=[])
    for prefix in (StoreCB.SEC_SALE, StoreCB.SEC_TRIAL, StoreCB.SEC_PANEL, StoreCB.SEC_GROUP):
        assert _has(kb, prefix)
    assert kb.inline_keyboard[-1][0].callback_data == StoreCB.HUB
    assert len(kb.inline_keyboard[-1]) == 1
    # A read-only viewer sees the plans but no editing section.
    ops = store_category_dashboard_keyboard(_cat(), is_owner=False, plan_buttons=[(1, "x")])
    assert not _has(ops, StoreCB.SEC_SALE)
    assert not _has(ops, StoreCB.PKG_ADD)


def test_dashboard_child_group_goes_back_to_its_parent() -> None:
    kb = store_category_dashboard_keyboard(_cat(9, parent_id=4), is_owner=True)
    assert kb.inline_keyboard[-1][0].callback_data == f"{StoreCB.CAT}4"


def test_reorder_button_needs_two_plans() -> None:
    one = store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=[(1, "a")])
    two = store_category_dashboard_keyboard(
        _cat(), is_owner=True, plan_buttons=[(1, "a"), (2, "b")]
    )
    assert not _has(one, StoreCB.SEC_ORDER)
    assert _has(two, StoreCB.SEC_ORDER)


def test_subgroup_add_follows_the_plans_or_children_rule() -> None:
    plans = [(1, "a")]
    assert not _has(
        store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=plans),
        StoreCB.CAT_SUB_ADD,
    )
    assert _has(
        store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=[]),
        StoreCB.CAT_SUB_ADD,
    )
    # One level of nesting only: a child cannot have children, so no button.
    assert not _has(
        store_category_dashboard_keyboard(_cat(9, parent_id=4), is_owner=True, plan_buttons=[]),
        StoreCB.CAT_SUB_ADD,
    )
    kid = SimpleNamespace(id=2, title_fa="ویژه", enabled=True)
    with_kids = store_category_dashboard_keyboard(_cat(), is_owner=True, children=[kid])
    assert not _has(with_kids, StoreCB.PKG_ADD)


# --------------------------------------------------------------------------- #
# Hide until relevant
# --------------------------------------------------------------------------- #


def test_tiers_and_durations_are_hidden_while_the_flat_list_is_on() -> None:
    off = store_cat_sale_keyboard(_cat(flat_plan_list=False))
    on = store_cat_sale_keyboard(_cat(flat_plan_list=True))
    assert _has(off, StoreCB.CAT_TIERS) and _has(off, StoreCB.CAT_DURS)
    assert not _has(on, StoreCB.CAT_TIERS)
    assert not _has(on, StoreCB.CAT_DURS)


def test_custom_device_limit_needs_custom_purchase_on_and_a_volume_group() -> None:
    for mode in ("everyone", "resellers"):
        assert _has(store_cat_sale_keyboard(_cat(custom_purchase_mode=mode)), StoreCB.CAT_HWID)
    assert not _has(store_cat_sale_keyboard(_cat(custom_purchase_mode="off")), StoreCB.CAT_HWID)
    # A group saved before the three states keeps its meaning.
    assert not _has(store_cat_sale_keyboard(_cat(allow_custom_purchase=False)), StoreCB.CAT_HWID)
    unlimited = _cat(kind="unlimited", custom_purchase_mode="everyone")
    assert not _has(store_cat_sale_keyboard(unlimited), StoreCB.CAT_HWID)


def test_an_unlimited_group_shows_no_custom_setting_but_a_volume_group_does() -> None:
    assert _has(store_cat_sale_keyboard(_cat(kind="volume")), StoreCB.CAT_CUSTOM)
    stored_on = _cat(kind="unlimited", custom_purchase_mode="everyone")
    assert not _has(store_cat_sale_keyboard(stored_on), StoreCB.CAT_CUSTOM)


def test_an_unlimited_group_dashboard_has_no_custom_status_line() -> None:
    from services.store_admin import CatalogView

    for kind, shown in (("volume", True), ("unlimited", False)):
        cat = SimpleNamespace(
            id=5,
            slug=kind,
            title_fa="گروه",
            enabled=True,
            parent_id=None,
            sort_order=1,
            panel_group_ids=[1],
            sales_flags={"kind": kind, "custom_purchase_mode": "everyone"},
        )
        view = CatalogView(version=None, is_draft=False, categories=[cat], packages=[])  # type: ignore[arg-type,list-item]
        body = ha._dashboard_text(view, cat)  # type: ignore[arg-type]
        assert ("خرید سفارشی" in body) is shown, (kind, body)


@pytest.mark.asyncio
async def test_a_new_unlimited_group_is_created_with_custom_off(session: AsyncSession) -> None:
    from services.store_catalog import custom_purchase_mode

    unlimited = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    volume = await create_draft_category(session, title_fa="حجمی", kind="volume")
    assert custom_purchase_mode(unlimited) == "off"
    assert custom_purchase_mode(volume) == "everyone"


def test_reseller_trial_switch_needs_a_trial() -> None:
    assert not _has(store_cat_trial_keyboard(_cat()), StoreCB.CAT_RTRIAL)
    with_trial = _cat(trial_gb=1, trial_days=1)
    assert _has(store_cat_trial_keyboard(with_trial), StoreCB.CAT_RTRIAL)
    assert _has(store_cat_trial_keyboard(with_trial), StoreCB.CAT_TRIAL)


def test_kind_needs_a_group_with_no_plans_and_no_children() -> None:
    cat = _cat()
    assert _has(store_cat_group_keyboard(cat), StoreCB.CAT_KIND)
    assert not _has(store_cat_group_keyboard(cat, has_plans=True), StoreCB.CAT_KIND)
    assert not _has(store_cat_group_keyboard(cat, has_children=True), StoreCB.CAT_KIND)


def test_group_section_subgroup_and_delete_rules() -> None:
    assert _has(store_cat_group_keyboard(_cat()), StoreCB.CAT_SUB_ADD)
    assert not _has(store_cat_group_keyboard(_cat(), has_plans=True), StoreCB.CAT_SUB_ADD)
    assert not _has(store_cat_group_keyboard(_cat(9, parent_id=4)), StoreCB.CAT_SUB_ADD)
    kb = store_cat_group_keyboard(_cat())
    delete_rows = [
        r
        for r in kb.inline_keyboard
        if _cbs(InlineKeyboardMarkup(inline_keyboard=[r]))[0].startswith(StoreCB.CAT_DEL)
    ]
    assert len(delete_rows) == 1 and len(delete_rows[0]) == 1
    assert delete_rows[0][0].style == style.DANGER
    # Never beside navigation.
    assert kb.inline_keyboard[-1][0].callback_data == f"{StoreCB.CAT}5"
    assert len(kb.inline_keyboard[-1]) == 1


def test_premium_panel_group_needs_tiers_on() -> None:
    assert not _has(store_cat_panel_keyboard(_cat()), StoreCB.CAT_TIER_GROUPS)
    assert _has(store_cat_panel_keyboard(_cat(tiers_enabled=True)), StoreCB.CAT_TIER_GROUPS)
    # …and only where tiers are read at all: not under the flat list.
    flat = _cat(tiers_enabled=True, flat_plan_list=True)
    assert not _has(store_cat_panel_keyboard(flat), StoreCB.CAT_TIER_GROUPS)
    assert _has(store_cat_panel_keyboard(_cat()), StoreCB.CAT_GROUPS)


def test_seed_shows_only_on_a_shop_with_no_groups() -> None:
    empty = store_hub_keyboard(is_owner=True, has_categories=False)
    full = store_hub_keyboard(is_owner=True, has_categories=True, groups=[(1, "حجمی")])
    assert StoreCB.SEED in _cbs(empty)
    assert StoreCB.SEED not in _cbs(full)
    assert StoreCB.SEED not in _cbs(store_hub_keyboard(is_owner=False, has_categories=False))


def test_arrows_exist_only_on_the_manual_order_screen() -> None:
    cat = _cat()
    plans = [(1, "a"), (2, "b")]
    manual = store_plan_order_keyboard(cat, plans, mode_label="m", manual=True)
    auto = store_plan_order_keyboard(cat, plans, mode_label="m", manual=False)
    assert _has(manual, StoreCB.PKG_UP) and _has(manual, StoreCB.PKG_DN)
    assert not _has(auto, StoreCB.PKG_UP) and not _has(auto, StoreCB.PKG_DN)
    assert _has(auto, StoreCB.PKG_ORDER)
    # Nowhere else.
    dash = store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=plans)
    detail = store_package_detail_keyboard(_pkg(), is_owner=True)
    for kb in (dash, detail):
        assert not _has(kb, StoreCB.PKG_UP) and not _has(kb, StoreCB.PKG_DN)


# --------------------------------------------------------------------------- #
# State + action labels
# --------------------------------------------------------------------------- #


def test_toggle_labels_show_the_state_and_the_action() -> None:
    on = _labels(store_cat_sale_keyboard(_cat(allow_new_sale=True, allow_renew=False)))
    assert "✅ فروش جدید" in on
    assert "❌ تمدید" in on
    flat = _labels(store_cat_sale_keyboard(_cat(flat_plan_list=True)))
    assert "✅ فهرست ساده" in flat
    for flags, label in (
        ({"custom_purchase_mode": "off"}, "❌ خرید سفارشی"),
        ({"custom_purchase_mode": "resellers"}, "🏪 خرید سفارشی: فقط نماینده‌ها"),
        ({"custom_purchase_mode": "everyone"}, "✅ خرید سفارشی: همه"),
        ({}, "✅ خرید سفارشی: همه"),  # never set: the old default was on
        ({"allow_custom_purchase": False}, "❌ خرید سفارشی"),  # saved before the three states
    ):
        assert label in _labels(store_cat_sale_keyboard(_cat(**flags))), flags
    tiers = _labels(store_cat_sale_keyboard(_cat(tiers_enabled=True)))
    assert "✅ تفکیک ویژه/اقتصادی" in tiers
    reseller = _labels(
        store_cat_trial_keyboard(_cat(trial_gb=1, trial_days=1, reseller_trial=True))
    )
    assert "✅ تست نماینده" in reseller


def test_plan_detail_toggles_are_state_plus_action_and_delete_is_last_but_one() -> None:
    kb = store_package_detail_keyboard(_pkg(allow_new_sale=False), is_owner=True)
    labels = _labels(kb)
    assert "❌ فروش جدید" in labels
    assert "✅ تمدید" in labels
    assert "✅ تخفیف اضافه" in labels
    delete_row = kb.inline_keyboard[-2]
    assert [b.callback_data for b in delete_row] == [f"{StoreCB.PKG_DEL}3"]
    assert delete_row[0].style == style.DANGER
    assert kb.inline_keyboard[-1][0].callback_data == f"{StoreCB.CAT}5"


# --------------------------------------------------------------------------- #
# One accent per screen
# --------------------------------------------------------------------------- #


def _every_store_keyboard() -> list[tuple[str, InlineKeyboardMarkup]]:
    plans = [(1, "a"), (2, "b")]
    kid = SimpleNamespace(id=2, title_fa="ویژه", enabled=True)
    return [
        ("hub-empty", store_hub_keyboard(is_owner=True, has_categories=False)),
        ("hub", store_hub_keyboard(is_owner=True, has_categories=True, groups=[(1, "x")])),
        ("dash-empty", store_category_dashboard_keyboard(_cat(), is_owner=True)),
        (
            "dash-plans",
            store_category_dashboard_keyboard(_cat(), is_owner=True, plan_buttons=plans),
        ),
        ("dash-kids", store_category_dashboard_keyboard(_cat(), is_owner=True, children=[kid])),
        ("sale", store_cat_sale_keyboard(_cat())),
        ("trial", store_cat_trial_keyboard(_cat(trial_gb=1, trial_days=1))),
        ("panel", store_cat_panel_keyboard(_cat(tiers_enabled=True))),
        ("group", store_cat_group_keyboard(_cat())),
        ("order", store_plan_order_keyboard(_cat(), plans, mode_label="m", manual=True)),
        ("detail", store_package_detail_keyboard(_pkg(), is_owner=True)),
    ]


@pytest.mark.parametrize(
    "name,kb", _every_store_keyboard(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_store_keyboard_has_more_than_one_accent(name: str, kb: InlineKeyboardMarkup) -> None:
    primaries = [b.text for row in kb.inline_keyboard for b in row if b.style == style.PRIMARY]
    assert len(primaries) <= 1, (name, primaries)


# --------------------------------------------------------------------------- #
# Handlers: render, tap, re-render the same section
# --------------------------------------------------------------------------- #


def _settings() -> SimpleNamespace:
    return SimpleNamespace(admin_telegram_ids=[OWNER_ID], store_owner_telegram_ids=[OWNER_ID])


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=OWNER_ID), answer=AsyncMock(), message=None
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


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: msg)
    return msg


def _last(screen: SimpleNamespace) -> tuple[str, InlineKeyboardMarkup]:
    call = screen.edit_text.await_args
    return call.args[0], call.kwargs["reply_markup"]


async def _group(session: AsyncSession) -> int:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await update_draft_category(session, cat.id, panel_group_ids="7")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    return int(view.categories[0].id)


async def _tap(handler: Any, data: str, session: AsyncSession) -> None:
    await handler(_callback(data), _settings(), None, session)


@pytest.mark.asyncio
async def test_a_toggle_flips_and_lands_back_on_the_sale_section(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _group(session)
    await _tap(ha.store_cat_toggle_ns, f"{StoreCB.CAT_NS}{cat_id}", session)
    body, kb = _last(screen)
    # Same section (sale), with the saved notice — not the dashboard, not the hub.
    assert body.startswith("✅") or "✅ ذخیره شد" in body
    assert "❌ فروش جدید" in _labels(kb)
    assert _has(kb, StoreCB.CAT_FLAT)
    # Back goes to the dashboard of the *live* row: publishing minted a new id.
    live_id = (await get_catalog_view(session)).categories[0].id
    assert kb.inline_keyboard[-1][0].callback_data == f"{StoreCB.CAT}{live_id}"

    await _tap(ha.store_cat_toggle_ns, f"{StoreCB.CAT_NS}{cat_id}", session)
    _body, kb = _last(screen)
    assert "✅ فروش جدید" in _labels(kb)


@pytest.mark.asyncio
async def test_flat_toggle_hides_tiers_on_the_re_render(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _group(session)
    await ha.store_cat_toggle_flat(
        _callback(f"{StoreCB.CAT_FLAT}{cat_id}"), _settings(), None, session
    )
    _body, kb = _last(screen)
    assert "✅ فهرست ساده" in _labels(kb)
    assert not _has(kb, StoreCB.CAT_TIERS)
    assert not _has(kb, StoreCB.CAT_DURS)


@pytest.mark.asyncio
async def test_the_reseller_toggle_returns_to_the_trial_section(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _group(session)
    await set_category_trial(session, cat_id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="تست")
    await _tap(ha.store_cat_toggle_reseller_trial, f"{StoreCB.CAT_RTRIAL}{cat_id}", session)
    body, kb = _last(screen)
    assert "✅" in body
    assert "✅ تست نماینده" in _labels(kb)
    assert _has(kb, StoreCB.CAT_TRIAL)


@pytest.mark.asyncio
async def test_the_kind_toggle_returns_to_the_group_section(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _group(session)
    await _tap(ha.store_cat_toggle_kind, f"{StoreCB.CAT_KIND}{cat_id}", session)
    body, kb = _last(screen)
    assert "✅" in body
    assert _has(kb, StoreCB.CAT_TITLE) and _has(kb, StoreCB.CAT_DEL)
    assert any(label.startswith("نوع: نامحدود") for label in _labels(kb))


@pytest.mark.asyncio
async def test_the_dashboard_shows_status_warnings_and_real_plan_buttons(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat_id = int(view.categories[0].id)
    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{cat_id}"), _settings(), None, session, _state()
    )
    body, kb = _last(screen)
    # No panel group and no plans: both are called out, and the status block is there.
    assert "⚠️ گروه پنل تنظیم نشده" in body
    assert "⚠️ هنوز پلنی نداره" in body
    for needle in ("فروش:", "گروه پنل:", "تست رایگان: خاموش", "شکل قفسه: تب مدت", "پلن‌ها: ۰"):
        assert needle in body
    assert not any("پلن‌های این گروه" in t for t in _labels(kb))

    await update_draft_category(session, cat.id, panel_group_ids="7")
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=90_000,
        volume_gb=50,
        duration_days=30,
    )
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۱۰۰ گیگ",
        fixed_price_toman=170_000,
        volume_gb=100,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    view = await get_catalog_view(session)
    live_id = int(view.categories[0].id)
    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{live_id}"), _settings(), None, session, _state()
    )
    body, kb = _last(screen)
    assert "⚠️" not in body
    labels = _labels(kb)
    assert any("۵۰ گیگ" in t and "۹۰ هزار" in t for t in labels)
    assert any("۱۰۰ گیگ" in t and "۱۷۰ هزار" in t for t in labels)
    assert _has(kb, StoreCB.SEC_ORDER)


@pytest.mark.asyncio
async def test_the_hub_marks_groups_and_offers_seed_only_when_empty(
    session: AsyncSession,
) -> None:
    empty_view = await get_catalog_view(session)
    _text, groups = ha._hub_summary(empty_view, is_owner=True)
    assert groups == []

    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    text, groups = ha._hub_summary(view, is_owner=True)
    assert "⚠️ ۱ گروه بدون گروه پنل" in text
    assert groups[0][1].startswith("⚠️")

    await update_draft_category(session, cat.id, panel_group_ids="7", allow_new_sale=False)
    await commit_and_activate(session, notes="پنل")
    view = await get_catalog_view(session)
    text, groups = ha._hub_summary(view, is_owner=True)
    assert "⚠️" not in text
    assert groups[0][1].startswith("⏸")
    kb = store_hub_keyboard(is_owner=True, has_categories=bool(view.categories), groups=groups)
    assert StoreCB.SEED not in _cbs(kb)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale",
    [StoreCB.ADV, StoreCB.STRAT, StoreCB.CATS, StoreCB.READY, StoreCB.CUSTOM, StoreCB.PKGS_PICK],
)
async def test_buttons_of_removed_screens_land_on_the_hub(
    stale: str, session: AsyncSession, screen: SimpleNamespace
) -> None:
    await _group(session)
    cb = _callback(stale)
    await ha.store_retired_to_hub(cb, _settings(), None, session, _state())
    _body, kb = _last(screen)
    assert StoreCB.DISC in _cbs(kb)
    assert cb.answer.await_count == 1
