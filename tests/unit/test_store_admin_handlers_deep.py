"""Deep handler-level bug hunt for «مدیریت فروشگاه».

Service-level tests cannot catch FSM hand-offs, stale callback ids after
publish, or copy that still promises a draft/publish ceremony. These walk the
real handlers the owner taps.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    get_draft_version,
    update_draft_category,
)
from services.store_strategy import list_store_changes

OWNER_ID = 4242


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        admin_telegram_ids=[OWNER_ID],
        store_owner_telegram_ids=[OWNER_ID],
    )


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _screen() -> SimpleNamespace:
    return SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())


def _callback(data: str, message: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=OWNER_ID),
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


def _assert_has_markup(mock: AsyncMock) -> None:
    assert mock.await_count >= 1
    for c in mock.await_args_list:
        kwargs = c.kwargs
        assert kwargs.get("reply_markup") is not None or (
            len(c.args) >= 2 and c.args[1] is not None
        ), f"admin reply without keyboard: {c}"


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


async def _seed_volume_group(session: AsyncSession, title: str = "حجمی") -> int:
    await create_draft_category(session, title_fa=title, kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    return view.categories[0].id


async def _seed_unlimited_group(session: AsyncSession) -> int:
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    return next(c.id for c in view.categories if c.title_fa == "نامحدود")


# --------------------------------------------------------------------------- #
# New-plan wizard — happy paths + nothing persisted before confirm
# --------------------------------------------------------------------------- #


async def _drive_wizard_to_confirm(
    state: FSMContext,
    session: AsyncSession,
    screen: SimpleNamespace,
    *,
    category_id: int,
    size: str | int,
    duration: str | int,
    price: str,
    kind_via_start: bool = True,
    hwid: str | int | None = 0,
) -> None:
    """Run size → duration → (users) → price so FSM holds a pending plan."""
    if kind_via_start:
        await ha.store_pkg_add_start(
            _callback(f"{StoreCB.PKG_ADD}{category_id}"),  # type: ignore[arg-type]
            _settings(),  # type: ignore[arg-type]
            None,
            session,
            state,
        )
    if isinstance(size, int):
        await ha.store_pkg_size_pick(
            _callback(f"{StoreCB.PKG_SIZE}{size}"),  # type: ignore[arg-type]
            _settings(),  # type: ignore[arg-type]
            None,
            state,
        )
    else:
        await ha.store_pkg_size_typed(
            _typed(size),  # type: ignore[arg-type]
            state,
            _settings(),  # type: ignore[arg-type]
            None,
        )
    if isinstance(duration, int):
        await ha.store_pkg_duration_pick(
            _callback(f"{StoreCB.PKG_DUR}{duration}"),  # type: ignore[arg-type]
            _settings(),  # type: ignore[arg-type]
            None,
            session,
            state,
        )
    else:
        await ha.store_pkg_duration_typed(
            _typed(duration),  # type: ignore[arg-type]
            state,
            session,
            _settings(),  # type: ignore[arg-type]
            None,
        )
    data = await state.get_data()
    if str(data.get("new_pkg_kind") or "volume") == "volume" and hwid is not None:
        if isinstance(hwid, int):
            await ha.store_pkg_users_pick(
                _callback(f"{StoreCB.PKG_USERS}{hwid}"),  # type: ignore[arg-type]
                _settings(),  # type: ignore[arg-type]
                None,
                session,
                state,
            )
        else:
            await ha.store_pkg_users_typed(
                _typed(hwid),  # type: ignore[arg-type]
                state,
                session,
                _settings(),  # type: ignore[arg-type]
                None,
            )
    await ha.store_pkg_add_price(
        _typed(price),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )


@pytest.mark.asyncio
async def test_volume_wizard_persists_nothing_until_confirm(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=cat_id, size=50, duration=30, price="150000"
    )

    assert await state.get_state() == AdminStates.store_waiting_new_pkg_confirm.state
    view = await get_catalog_view(session)
    assert view.packages == [], "half-built plan must not ride a later publish"
    assert await get_draft_version(session) is None
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_volume_wizard_confirm_writes_options_and_clears_fsm(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=cat_id, size=50, duration=30, price="۱۵۰٬۰۰۰"
    )

    await ha.store_pkg_activate(
        _callback(StoreCB.PKG_ACTIVATE),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )

    assert await state.get_state() is None
    view = await get_catalog_view(session)
    assert len(view.packages) == 1
    pkg = view.packages[0]
    assert pkg.options.get("volume_gb") == 50
    assert pkg.options.get("duration_days") == 30
    assert pkg.fixed_price_toman == 150_000
    assert pkg.sales_flags.get("has_time_limit") is True
    _assert_has_markup(screen.edit_text)

    entries = await list_store_changes(session)
    assert entries[0].description.startswith("پلن «")
    assert any("\u0600" <= ch <= "\u06ff" for ch in entries[0].description)


@pytest.mark.asyncio
async def test_unlimited_wizard_confirm_writes_user_dims(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_unlimited_group(session)
    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=cat_id, size=2, duration=3, price="450000"
    )
    await ha.store_pkg_activate(
        _callback(StoreCB.PKG_ACTIVATE),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    pkg = (await get_catalog_view(session)).packages[0]
    assert pkg.options.get("concurrent_users") == 2
    assert pkg.options.get("duration_months") == 3
    assert pkg.fixed_price_toman == 450_000


@pytest.mark.asyncio
async def test_wizard_discard_clears_fsm_and_writes_nothing(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=cat_id, size=50, duration=30, price="100000"
    )
    await ha.store_pkg_discard_new(
        _callback(StoreCB.PKG_DISCARD),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    assert await state.get_state() is None
    assert (await get_catalog_view(session)).packages == []
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_typed_wizard_rejects_invalid_numbers(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await ha.store_pkg_add_start(
        _callback(f"{StoreCB.PKG_ADD}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    for bad in ("", "abc", "-5", "0", "  ", "۵۰ خیلی", "50 30"):
        msg = _typed(bad)
        await ha.store_pkg_size_typed(
            msg,  # type: ignore[arg-type]
            state,
            _settings(),  # type: ignore[arg-type]
            None,
        )
        assert await state.get_state() == AdminStates.store_waiting_new_pkg_size.state
        assert texts.admin_store_number_invalid.split("{")[0] in (
            msg.answer.await_args.args[0]
            if msg.answer.await_args.args
            else msg.answer.await_args.kwargs.get("text", "")
        ) or "عدد" in str(msg.answer.await_args)
        _assert_has_markup(msg.answer)
        # English exception text must never leak
        answered = str(msg.answer.await_args)
        assert "invalid literal" not in answered
        assert "ValueError" not in answered


@pytest.mark.asyncio
async def test_typed_size_accepts_persian_digits(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await ha.store_pkg_add_start(
        _callback(f"{StoreCB.PKG_ADD}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_size_typed(
        _typed("۵۰"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    data = await state.get_data()
    assert data["new_pkg_size_pending"] == 50
    assert await state.get_state() == AdminStates.store_waiting_new_pkg_duration.state


@pytest.mark.asyncio
async def test_price_step_rejects_zero_and_letters(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await state.update_data(
        new_pkg_category_id=cat_id,
        new_pkg_kind="volume",
        new_pkg_size=50,
        new_pkg_duration=30,
        new_pkg_title="۵۰ گیگ / ۳۰ روز",
    )
    await state.set_state(AdminStates.store_waiting_new_pkg_price)
    for bad in ("0", "-1", "abc", ""):
        msg = _typed(bad)
        await ha.store_pkg_add_price(
            msg,  # type: ignore[arg-type]
            state,
            _settings(),  # type: ignore[arg-type]
            None,
            session,
        )
        assert await state.get_state() == AdminStates.store_waiting_new_pkg_price.state
        _assert_has_markup(msg.answer)


# --------------------------------------------------------------------------- #
# Category flows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_top_level_category(session: AsyncSession, screen: SimpleNamespace) -> None:
    state = _state()
    await ha.store_cat_add_start(
        _callback(StoreCB.CAT_ADD),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_cat_add_title(
        _typed("حجمی اقتصادی"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_cat_add_kind(
        _callback(f"{StoreCB.CAT_ADD_KIND}volume"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    view = await get_catalog_view(session)
    assert any(c.title_fa == "حجمی اقتصادی" for c in view.categories)
    assert await get_draft_version(session) is None
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_create_child_under_parent(session: AsyncSession, screen: SimpleNamespace) -> None:
    parent_id = await _seed_volume_group(session)
    state = _state()
    await ha.store_sub_cat_start(
        _callback(f"{StoreCB.CAT_SUB_ADD}{parent_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_sub_cat_title(
        _typed("ویژه"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    view = await get_catalog_view(session)
    parent = view.category(parent_id)
    assert parent is not None
    children = view.children_of(parent.id)
    assert len(children) == 1
    assert children[0].title_fa == "ویژه"
    assert children[0].parent_id == parent.id


@pytest.mark.asyncio
async def test_third_level_category_is_refused(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    parent_id = await _seed_volume_group(session)
    state = _state()
    await ha.store_sub_cat_start(
        _callback(f"{StoreCB.CAT_SUB_ADD}{parent_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_sub_cat_title(
        _typed("ویژه"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    view = await get_catalog_view(session)
    child = view.children_of(view.category(parent_id).id)[0]  # type: ignore[union-attr]

    state2 = _state()
    await ha.store_sub_cat_start(
        _callback(f"{StoreCB.CAT_SUB_ADD}{child.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state2,
    )
    msg = _typed("طلایی")
    await ha.store_sub_cat_title(
        msg,  # type: ignore[arg-type]
        state2,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    answered = msg.answer.await_args.args[0]
    assert "یک سطح" in answered or "زیرگروه" in answered
    _assert_has_markup(msg.answer)
    view2 = await get_catalog_view(session)
    assert view2.children_of(child.id) == []


@pytest.mark.asyncio
async def test_cannot_add_child_when_group_has_plans(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    view = await get_catalog_view(session)
    cat = view.categories[0]

    state = _state()
    await ha.store_sub_cat_start(
        _callback(f"{StoreCB.CAT_SUB_ADD}{cat.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    msg = _typed("اقتصادی")
    await ha.store_sub_cat_title(
        msg,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    assert "پلن" in msg.answer.await_args.args[0]
    _assert_has_markup(msg.answer)


@pytest.mark.asyncio
async def test_cannot_add_plan_when_group_has_children(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    parent_id = await _seed_volume_group(session)
    await create_draft_category(session, title_fa="ویژه", kind="volume", parent_id=parent_id)
    await commit_and_activate(session, notes="زیرگروه ساخته شد")
    view = await get_catalog_view(session)
    parent = next(c for c in view.categories if c.parent_id is None)

    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=parent.id, size=50, duration=30, price="100000"
    )
    cb = _callback(StoreCB.PKG_ACTIVATE)
    await ha.store_pkg_activate(
        cb,  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    # Alert on the callback, or edit with the service error
    alerted = any(
        (c.kwargs.get("show_alert") and "زیرگروه" in str(c)) for c in cb.answer.await_args_list
    )
    edited = any("زیرگروه" in str(c) for c in screen.edit_text.await_args_list)
    assert alerted or edited
    assert (await get_catalog_view(session)).packages == []


@pytest.mark.asyncio
async def test_rename_category_commits_persian_notes(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    state = _state()
    await ha.store_cat_edit_title(
        _callback(f"{StoreCB.CAT_TITLE}{cat_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_cat_fsm_input(
        _typed("حجمی ویژه"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    view = await get_catalog_view(session)
    assert view.category(cat_id).title_fa == "حجمی ویژه"  # type: ignore[union-attr]
    entries = await list_store_changes(session)
    assert entries[0].description == "گروه محصول ویرایش شد"
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_delete_category_confirm_and_apply(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه ۱")
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه ۲")
    view = await get_catalog_view(session)
    doomed = next(c for c in view.categories if c.title_fa == "نامحدود")

    await ha.store_cat_delete_confirm(
        _callback(f"{StoreCB.CAT_DEL}{doomed.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    body = screen.edit_text.await_args.args[0]
    assert "حذف" in body
    # Must not promise a separate publish step
    assert "انتشار" not in body
    _assert_has_markup(screen.edit_text)

    await ha.store_cat_delete_apply(
        _callback(f"{StoreCB.CAT_DEL_OK}{doomed.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    titles = {c.title_fa for c in (await get_catalog_view(session)).categories}
    assert "نامحدود" not in titles
    assert "حجمی" in titles


@pytest.mark.asyncio
async def test_delete_nonempty_group_states_plan_scope(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    # Need a second root so delete is allowed
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه ۲")
    view = await get_catalog_view(session)
    cat = next(c for c in view.categories if c.title_fa == "حجمی")

    await ha.store_cat_delete_confirm(
        _callback(f"{StoreCB.CAT_DEL}{cat.id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    body = screen.edit_text.await_args.args[0]
    assert "پلن" in body


# --------------------------------------------------------------------------- #
# Stale-id resilience (publish between render and tap)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_category_button_still_opens_detail(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    old_id = await _seed_volume_group(session)
    # Another edit mints new row ids
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه دوم")
    view = await get_catalog_view(session)
    assert view.category(old_id) is not None, "slug fallback must find حجمی"
    new_id = view.category(old_id).id  # type: ignore[union-attr]
    assert new_id != old_id

    await ha.store_cat_detail(
        _callback(f"{StoreCB.CAT}{old_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    body = screen.edit_text.await_args.args[0]
    assert "حجمی" in body
    assert "یافت نشد" not in body
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_stale_pkgs_list_button_lands_on_the_group_dashboard(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    old_cat = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=old_cat,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    # Bump version so cat id changes
    view_mid = await get_catalog_view(session)
    pkg = view_mid.packages[0]
    await update_draft_category(session, pkg.category_id, title_fa="حجمی")
    await commit_and_activate(session, notes="گروه محصول ویرایش شد")

    await ha.store_retired_to_group(
        _callback(f"{StoreCB.PKGS}{old_cat}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    body = screen.edit_text.await_args.args[0]
    # Must not be empty / not-found
    assert "یافت نشد" not in body
    markup = screen.edit_text.await_args.kwargs.get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("۵۰ گیگ" in t for t in labels)


@pytest.mark.asyncio
async def test_stale_package_detail_does_not_create_stray_draft(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """Browsing a plan must not invent a draft the owner never asked for."""
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    view = await get_catalog_view(session)
    old_pkg_id = view.packages[0].id

    await update_draft_category(session, view.packages[0].category_id, title_fa="حجمی")
    await commit_and_activate(session, notes="گروه محصول ویرایش شد")
    assert await get_draft_version(session) is None

    await ha.store_pkg_detail(
        _callback(f"{StoreCB.PKG}{old_pkg_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    assert await get_draft_version(session) is None, "opening a plan must stay read-only"
    body = screen.edit_text.await_args.args[0]
    assert "۵۰ گیگ" in body
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_wizard_survives_stale_category_id_in_fsm(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """Start wizard, publish another edit, then confirm — plan must land."""
    old_cat = await _seed_volume_group(session)
    state = _state()
    await _drive_wizard_to_confirm(
        state, session, screen, category_id=old_cat, size=50, duration=30, price="120000"
    )
    # Unrelated publish while confirm screen is open
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه دوم")

    await ha.store_pkg_activate(
        _callback(StoreCB.PKG_ACTIVATE),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    pkgs = (await get_catalog_view(session)).packages
    assert len(pkgs) == 1
    assert pkgs[0].fixed_price_toman == 120_000


@pytest.mark.asyncio
async def test_child_under_stale_parent_id_links_to_current_parent(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """FSM may hold a pre-publish parent id; the child must not orphan."""
    old_parent = await _seed_volume_group(session)
    # Bump ids
    await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await commit_and_activate(session, notes="گروه دوم")
    view = await get_catalog_view(session)
    current_parent = view.category(old_parent)
    assert current_parent is not None
    assert current_parent.id != old_parent

    state = _state()
    await state.update_data(sub_cat_parent_id=old_parent)
    await state.set_state(AdminStates.store_waiting_sub_cat_title)
    await ha.store_sub_cat_title(
        _typed("ویژه"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        None,
    )
    view2 = await get_catalog_view(session)
    child = next(c for c in view2.categories if c.title_fa == "ویژه")
    live_parent = view2.category(old_parent)
    assert live_parent is not None
    assert child.parent_id == live_parent.id, (
        f"child.parent_id={child.parent_id} must point at live parent "
        f"{live_parent.id} (title={live_parent.title_fa}), not an orphan id"
    )
    assert child in view2.children_of(live_parent.id)


# --------------------------------------------------------------------------- #
# Callback parsing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_malformed_category_callbacks_answer_gracefully(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await _seed_volume_group(session)
    for data in (StoreCB.CAT, f"{StoreCB.CAT}abc", f"{StoreCB.CAT}-1", f"{StoreCB.CAT}999999"):
        cb = _callback(data)
        await ha.store_cat_detail(
            cb,  # type: ignore[arg-type]
            _settings(),  # type: ignore[arg-type]
            None,
            session,
            _state(),
        )
        assert cb.answer.await_count >= 1
        if data.endswith("999999"):
            body = screen.edit_text.await_args.args[0]
            assert "یافت نشد" in body
            _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_malformed_pkg_callback_shows_stale(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cb = _callback(f"{StoreCB.PKG}nope")
    await ha.store_pkg_detail(
        cb,  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        _state(),
    )
    cb.answer.assert_any_await(texts.stale_button, show_alert=True)


@pytest.mark.asyncio
async def test_pkg_add_with_missing_category_alerts(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cb = _callback(f"{StoreCB.PKG_ADD}999999")
    state = _state()
    await ha.store_pkg_add_start(
        cb,  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    cb.answer.assert_awaited_with(texts.admin_store_lost_context, show_alert=True)


# --------------------------------------------------------------------------- #
# Edit flows — Persian notes + Persian digits on price
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edit_plan_price_accepts_persian_digits(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    pkg_id = (await get_catalog_view(session)).packages[0].id

    state = _state()
    await ha.store_pkg_edit_price(
        _callback(f"{StoreCB.PKG_PRICE}{pkg_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
        session,
    )
    msg = _typed("۱۸۰٬۰۰۰")
    await ha.store_pkg_fsm_input(
        msg,  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    pkg = (await get_catalog_view(session)).packages[0]
    assert pkg.fixed_price_toman == 180_000
    entries = await list_store_changes(session)
    assert entries[0].description == "پلن ویرایش شد"
    _assert_has_markup(msg.answer)


@pytest.mark.asyncio
async def test_edit_plan_title_commits_persian_notes(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    pkg_id = (await get_catalog_view(session)).packages[0].id
    state = _state()
    await ha.store_pkg_edit_title(
        _callback(f"{StoreCB.PKG_TITLE}{pkg_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    msg = _typed("پلن ویژه ۵۰")
    await ha.store_pkg_fsm_input(
        msg,  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    assert (await get_catalog_view(session)).packages[0].title_fa == "پلن ویژه ۵۰"
    _assert_has_markup(msg.answer)


@pytest.mark.asyncio
async def test_edit_plan_dims_commits_with_persian_notes(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن ساخته شد")
    pkg_id = (await get_catalog_view(session)).packages[0].id
    state = _state()
    await ha.store_pkg_dims_start(
        _callback(f"{StoreCB.PKG_DIMS}{pkg_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    await ha.store_pkg_size_pick(
        _callback(f"{StoreCB.PKG_SIZE}100"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        state,
    )
    await ha.store_pkg_duration_pick(
        _callback(f"{StoreCB.PKG_DUR}60"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
        state,
    )
    pkg = (await get_catalog_view(session)).packages[0]
    assert pkg.options.get("volume_gb") == 100
    assert pkg.options.get("duration_days") == 60
    entries = await list_store_changes(session)
    assert entries[0].description == "حجم و مدت پلن عوض شد"


@pytest.mark.asyncio
async def test_plan_order_toggle_commits_persian_notes(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    cat_id = await _seed_volume_group(session)
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن")
    live_id = (await get_catalog_view(session)).categories[0].id
    await ha.store_pkg_order_toggle(
        _callback(f"{StoreCB.PKG_ORDER}{live_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    entries = await list_store_changes(session)
    assert "ترتیب" in entries[0].description
    assert any("\u0600" <= ch <= "\u06ff" for ch in entries[0].description)


# --------------------------------------------------------------------------- #
# Undo screens
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_undo_confirm_shows_undoes_also(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۵۰ گیگ / ۳۰ روز» ساخته شد")
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۱۰۰ گیگ / ۳۰ روز",
        fixed_price_toman=250_000,
        volume_gb=100,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۱۰۰ گیگ / ۳۰ روز» ساخته شد")

    entries = await list_store_changes(session)
    oldest = entries[-1]
    assert oldest.undoes_also == 2

    await ha.store_change_undo_ask(
        _callback(f"{StoreCB.HIST_REST}{oldest.version_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    body = screen.edit_text.await_args.args[0]
    assert "۲" in body or "2" in body  # undoes_also rendered (Persian preferred)
    assert texts.admin_store_undo_also.split("{")[0][:10] in body or "برمی‌گرده" in body
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_undo_newest_via_handler(session: AsyncSession, screen: SimpleNamespace) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۵۰ گیگ / ۳۰ روز» ساخته شد")
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۱۰۰ گیگ / ۳۰ روز",
        fixed_price_toman=250_000,
        volume_gb=100,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۱۰۰ گیگ / ۳۰ روز» ساخته شد")

    entries = await list_store_changes(session)
    newest = entries[0]
    await ha.store_history_restore_ok(
        _callback(f"{StoreCB.HIST_OK}{newest.version_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    titles = [p.title_fa for p in (await get_catalog_view(session)).packages]
    assert titles == ["۵۰ گیگ / ۳۰ روز"]
    _assert_has_markup(screen.edit_text)


@pytest.mark.asyncio
async def test_undo_middle_entry_via_handler(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    await create_draft_category(session, title_fa="حجمی", kind="volume")
    await commit_and_activate(session, notes="گروه محصول ساخته شد")
    view = await get_catalog_view(session)
    cat_id = view.categories[0].id
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۵۰ گیگ / ۳۰ روز",
        fixed_price_toman=150_000,
        volume_gb=50,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۵۰ گیگ / ۳۰ روز» ساخته شد")
    await create_draft_package(
        session,
        category_id=cat_id,
        title_fa="۱۰۰ گیگ / ۳۰ روز",
        fixed_price_toman=250_000,
        volume_gb=100,
        duration_days=30,
    )
    await commit_and_activate(session, notes="پلن «۱۰۰ گیگ / ۳۰ روز» ساخته شد")

    entries = await list_store_changes(session)
    middle = entries[1]  # undoes the newest too
    await ha.store_history_restore_ok(
        _callback(f"{StoreCB.HIST_OK}{middle.version_id}"),  # type: ignore[arg-type]
        _settings(),  # type: ignore[arg-type]
        None,
        session,
    )
    assert (await get_catalog_view(session)).packages == []


# --------------------------------------------------------------------------- #
# Stale draft/publish copy still shown to the owner
# --------------------------------------------------------------------------- #


def test_delete_confirm_copy_must_not_promise_a_publish_step() -> None:
    """Every edit goes live on confirm — promising «انتشار» is a lie."""
    for attr in (
        "admin_store_cat_del_confirm",
        "admin_store_pkg_del_confirm",
        "admin_store_cat_deleted",
        "admin_store_pkg_deleted",
    ):
        text = getattr(texts, attr)
        assert "انتشار" not in text, attr
        assert "پیش‌نویس" not in text, attr
