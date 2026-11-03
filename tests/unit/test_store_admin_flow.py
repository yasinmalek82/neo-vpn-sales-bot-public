"""Approved store-admin UX redesign — handler + keyboard contracts.

Mutating the real production line must turn these red. Patterns match
``test_store_admin_handlers_deep.py``.
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import (
    StoreCB,
    store_cat_sale_keyboard,
    store_category_dashboard_keyboard,
    store_hub_keyboard,
    store_package_detail_keyboard,
    store_plan_order_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models.store import StoreVersion
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    format_preview_fa,
    get_catalog_view,
    preview_catalog,
)
from services.store_catalog import plan_order_mode

OWNER_ID = 4242
OPS_ID = 9999


def _settings(*, owner: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        admin_telegram_ids=[OWNER_ID, OPS_ID],
        store_owner_telegram_ids=[OWNER_ID] if owner else [],
    )


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _screen() -> SimpleNamespace:
    return SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())


def _callback(
    data: str,
    message: SimpleNamespace | None = None,
    *,
    user_id: int = OWNER_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        message=message,
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


def _kb_labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _kb_cbs(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _last_markup(mock: AsyncMock):
    assert mock.await_count >= 1
    call = mock.await_args_list[-1]
    return call.kwargs.get("reply_markup") or (call.args[1] if len(call.args) > 1 else None)


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
    msg = _screen()
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: msg)
    return msg


async def _seed_parent_with_children(session: AsyncSession) -> tuple[int, int, int]:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    parent_id = view.categories[0].id
    await create_draft_category(session, title_fa="ویژه", kind="volume", parent_id=parent_id)
    await commit_and_activate(session, notes="زیرگروه ویژه")
    view = await get_catalog_view(session)
    parent = next(c for c in view.categories if c.parent_id is None)
    await create_draft_category(session, title_fa="اقتصادی", kind="volume", parent_id=parent.id)
    await commit_and_activate(session, notes="زیرگروه اقتصادی")
    view = await get_catalog_view(session)
    parent = next(c for c in view.categories if c.parent_id is None)
    kids = [c for c in view.categories if c.parent_id == parent.id]
    return parent.id, kids[0].id, kids[1].id


async def _version_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(StoreVersion)) or 0)


# --------------------------------------------------------------------------- #
# 1. Child-group navigation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_parent_with_children_renders_child_buttons_owner_and_ops(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    parent_id, kid_a, kid_b = await _seed_parent_with_children(session)
    view = await get_catalog_view(session)
    parent = view.category(parent_id)
    assert parent is not None
    children = view.children_of(parent.id)

    for is_owner in (True, False):
        kb = store_category_dashboard_keyboard(parent, is_owner=is_owner, children=children)
        labels = _kb_labels(kb)
        cbs = _kb_cbs(kb)
        assert any("ویژه" in t for t in labels)
        assert any("اقتصادی" in t for t in labels)
        assert f"{StoreCB.CAT}{kid_a}" in cbs or f"{StoreCB.CAT}{kid_b}" in cbs
        assert not any("پلن‌های این گروه" in t for t in labels)
        assert StoreCB.PKGS + str(parent.id) not in cbs

    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{parent_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    markup = _last_markup(screen.edit_text)
    assert any("ویژه" in t for t in _kb_labels(markup))

    # Child button opens child detail
    child = children[0]
    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{child.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    body = screen.edit_text.await_args.args[0]
    assert child.title_fa in body


# --------------------------------------------------------------------------- #
# 2. Slim detail + settings + add-button gates
# --------------------------------------------------------------------------- #


def test_empty_group_shows_both_add_buttons() -> None:
    cat = SimpleNamespace(id=1, enabled=True, parent_id=None, sales_flags={"kind": "volume"})
    kb = store_category_dashboard_keyboard(cat, is_owner=True, children=[], plan_buttons=[])
    labels = _kb_labels(kb)
    assert any("پلن جدید" in t for t in labels)
    assert any("زیرگروه جدید" in t for t in labels)
    primaries = [b for row in kb.inline_keyboard for b in row if b.style is not None]
    assert sum(1 for b in primaries if "پلن جدید" in (b.text or "")) == 1


def test_group_with_plans_hides_sub_add() -> None:
    cat = SimpleNamespace(id=1, enabled=True, parent_id=None, sales_flags={})
    kb = store_category_dashboard_keyboard(
        cat, is_owner=True, children=[], plan_buttons=[(9, "۵۰ گیگ – ۱ ماهه | ۹۰ هزار")]
    )
    labels = _kb_labels(kb)
    assert any("پلن جدید" in t for t in labels)
    assert not any("زیرگروه جدید" in t for t in labels)


def test_group_with_children_hides_plan_add() -> None:
    cat = SimpleNamespace(id=1, enabled=True, parent_id=None, sales_flags={})
    kid = SimpleNamespace(id=2, title_fa="ویژه", enabled=True)
    kb = store_category_dashboard_keyboard(cat, is_owner=True, children=[kid])
    labels = _kb_labels(kb)
    assert not any("پلن جدید" in t for t in labels)
    assert any("زیرگروه جدید" in t for t in labels)
    assert not any("پلن‌های این گروه" in t for t in labels)


@pytest.mark.asyncio
async def test_toggle_cat_flag_re_renders_its_section_not_the_dashboard(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """Old bug: re-render passed neither has_children nor has_plans → both ➕."""
    parent_id, _, _ = await _seed_parent_with_children(session)
    view = await get_catalog_view(session)
    parent = view.category(parent_id)
    assert parent is not None

    await ha.store_cat_toggle_en(
        _callback(f"{StoreCB.CAT_EN}{parent.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    markup = _last_markup(screen.edit_text)
    labels = _kb_labels(markup)
    cbs = _kb_cbs(markup)
    body = screen.edit_text.await_args.args[0]
    # The sale section (back to the dashboard), not the dashboard with both add buttons
    assert any(c.startswith(StoreCB.CAT) and c[len(StoreCB.CAT) :].isdigit() for c in cbs)
    assert texts.admin_store_cat_settings_hint.split("؛")[0] in body
    assert not any("پلن جدید" in t for t in labels)
    assert not any("زیرگروه جدید" in t for t in labels)


@pytest.mark.asyncio
async def test_stale_settings_button_lands_on_the_dashboard(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat = view.categories[0]
    await ha.store_retired_to_group(
        _callback(f"{StoreCB.CAT_SET}{cat.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    markup = _last_markup(screen.edit_text)
    cbs = _kb_cbs(markup)
    assert f"{StoreCB.SEC_SALE}{cat.id}" in cbs
    assert StoreCB.HUB in cbs


# --------------------------------------------------------------------------- #
# 3. Package toggle preserves origin
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pkg_toggle_preserves_ready_origin(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat = view.categories[0]
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    view = await get_catalog_view(session)
    pkg = view.packages[0]

    await ha.store_pkg_en(
        _callback(f"{StoreCB.PKG_EN}{pkg.id}:r"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    markup = _last_markup(screen.edit_text)
    # «همهٔ پلن‌ها» is gone, so an old origin falls back to the hub, not a dead list.
    assert StoreCB.HUB in _kb_cbs(markup)
    # Toggle callback itself still carries origin for the next tap
    assert any(c.endswith(":r") for c in _kb_cbs(markup) if c.startswith(StoreCB.PKG_EN))


# --------------------------------------------------------------------------- #
# 4. FSM hygiene on navigation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cats_and_ready_clear_pending_pkg_price_state(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    state = _state()
    await state.set_state(AdminStates.store_waiting_new_pkg_price)
    await state.update_data(pending_price=1)

    await ha.store_retired_to_hub(
        _callback(StoreCB.CATS),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    assert await state.get_state() is None

    await state.set_state(AdminStates.store_waiting_new_pkg_price)
    await ha.store_retired_to_hub(
        _callback(StoreCB.READY),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    assert await state.get_state() is None


# --------------------------------------------------------------------------- #
# 5. Hub + advanced reshape
# --------------------------------------------------------------------------- #


def test_hub_carries_the_tools_advanced_used_to_hold() -> None:
    hub = store_hub_keyboard(is_owner=True, has_categories=True, groups=[(3, "⏸ حجمی")])
    hub_cbs = _kb_cbs(hub)
    assert StoreCB.HIST in hub_cbs
    assert StoreCB.DISC in hub_cbs
    assert StoreCB.PAY in hub_cbs
    assert f"{StoreCB.CAT}3" in hub_cbs
    for gone in (StoreCB.CUSTOM, StoreCB.READY, StoreCB.ADV, StoreCB.CATS):
        assert gone not in hub_cbs
    assert not any("همهٔ پلن‌ها" in t or "پیشرفته" in t for t in _kb_labels(hub))


@pytest.mark.asyncio
async def test_strat_lands_on_the_hub(session: AsyncSession, screen: SimpleNamespace) -> None:
    await ha.store_retired_to_hub(
        _callback(StoreCB.STRAT),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    markup = _last_markup(screen.edit_text)
    assert StoreCB.DISC in _kb_cbs(markup)
    assert StoreCB.PAY in _kb_cbs(markup)


@pytest.mark.asyncio
async def test_browsing_the_dashboard_and_hub_invents_no_draft(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat = view.categories[0]
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="۵۰ گیگ",
        fixed_price_toman=90_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    before = await _version_count(session)

    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{cat.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    labels = _kb_labels(_last_markup(screen.edit_text))
    assert any("۵۰ گیگ" in t and "۹۰ هزار" in t for t in labels)
    assert await _version_count(session) == before
    await ha.store_retired_to_hub(
        _callback(StoreCB.READY),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    assert await _version_count(session) == before

    await ha.store_preview(
        _callback(StoreCB.PREVIEW),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    assert await _version_count(session) == before

    await ha.store_discount_hub(
        _callback(StoreCB.DISC),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    assert await _version_count(session) == before

    await ha.store_promo_list(
        _callback(StoreCB.PROMO),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    assert await _version_count(session) == before


# --------------------------------------------------------------------------- #
# 6. Truthful preview
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preview_says_active_version_not_draft(session: AsyncSession) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    text = format_preview_fa(preview_catalog(view))
    assert "نسخهٔ فعال" in text
    assert "پیش‌نویس" not in text
    assert "منتشر نشده" not in text


# --------------------------------------------------------------------------- #
# 7. Steps retired vs STRAT
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_steps_prefix_retired_and_strat_lands_on_the_hub(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cb = _callback("adm:st:sta")
    await ha.store_steps_retired(cb)  # type: ignore[arg-type]
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert texts.admin_store_steps_retired in cb.answer.await_args.args[0]

    await ha.store_retired_to_hub(
        _callback(StoreCB.STRAT),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    assert StoreCB.DISC in _kb_cbs(_last_markup(screen.edit_text))


# --------------------------------------------------------------------------- #
# 9. Small fixes: price parse, single PKG_ORDER
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pkg_price_input_refuses_zero_and_garbage(
    session: AsyncSession,
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    view = await get_catalog_view(session)
    cat = view.categories[0]
    await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="پلن",
        fixed_price_toman=100_000,
        volume_gb=10,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    view = await get_catalog_view(session)
    pkg = view.packages[0]

    for bad in ("۰", "abc"):
        state = _state()
        await state.set_state(AdminStates.store_waiting_pkg_price)
        await state.update_data(store_pkg_id=pkg.id)
        msg = _typed(bad)
        await ha.store_pkg_fsm_input(
            msg,  # type: ignore[arg-type]
            state,
            _settings(),  # type: ignore[arg-type]
            None,
            session,
        )
        assert texts.admin_store_invalid_input in msg.answer.await_args.args[0]
        assert await state.get_state() == AdminStates.store_waiting_pkg_price.state

    state = _state()
    await state.set_state(AdminStates.store_waiting_pkg_price)
    await state.update_data(store_pkg_id=pkg.id)
    msg = _typed("۱۵۰٬۰۰۰")
    await ha.store_pkg_fsm_input(
        msg,  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    view = await get_catalog_view(session)
    assert view.package(pkg.id).fixed_price_toman == 150_000  # type: ignore[union-attr]


def test_exactly_one_pkg_order_button() -> None:
    cat = SimpleNamespace(
        id=7,
        sales_flags={"kind": "volume", "plan_order_mode": "auto"},
    )
    assert plan_order_mode(cat) == "auto"
    kb = store_plan_order_keyboard(cat, [], mode_label="ترتیب: قیمت ↔ دستی", manual=False)
    order_cbs = [c for c in _kb_cbs(kb) if c.startswith(StoreCB.PKG_ORDER)]
    assert len(order_cbs) == 1


def test_package_detail_toggle_callbacks_carry_origin() -> None:
    pkg = SimpleNamespace(
        id=3,
        kind=SimpleNamespace(value="ready"),
        enabled=True,
        allow_new_sale=True,
        allow_renew=True,
        forbid_extra_discount=False,
        category_id=1,
        sales_flags={},
        options={"volume_gb": 10, "duration_days": 30},
    )
    kb = store_package_detail_keyboard(pkg, is_owner=True, back_cb=StoreCB.READY)
    assert f"{StoreCB.PKG_EN}3:r" in _kb_cbs(kb)


def test_settings_keyboard_exists() -> None:
    cat = SimpleNamespace(
        id=5,
        enabled=True,
        sales_flags={"kind": "volume", "allow_new_sale": True, "allow_renew": True},
    )
    kb = store_cat_sale_keyboard(cat)
    assert f"{StoreCB.CAT}{cat.id}" in _kb_cbs(kb)
