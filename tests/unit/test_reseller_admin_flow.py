"""The admin side of resellers, driven through its real handlers.

Handler-level on purpose. The one defect that made the whole feature useless —
a guard rewriting the agreed price back to retail — lived in the wiring *past*
a correct unit, and no service-level test could ever have seen it. These drive
the callbacks the owner taps.
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

import database.models  # noqa: F401  (registers mappers)
from bot.handlers.admin import (
    _ready_plans_by_group,
    _role_label,
    admin_reseller_plan_ask,
    admin_reseller_plan_clear,
    admin_reseller_rate_ask,
    admin_reseller_section,
    admin_revoke_reseller,
    admin_toggle_reseller,
)
from bot.keyboards.admin import AdminCB
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models import User
from services.reseller import get_plan_price, get_rate, list_plan_prices, set_plan_price
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
)

ADMIN_ID = 4242


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def shop(session: AsyncSession) -> User:
    user = User(telegram_id=700, first_name="موبایل ایران", referral_code="S1")
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def catalog(session: AsyncSession) -> dict[str, str]:
    """Two groups selling the same size — the case a flat list cannot render."""
    slugs: dict[str, str] = {}
    for group, plans in (
        ("حجمی ویژه", [("۵۰ گیگ / ۳۰ روز", 150_000), ("۱۰۰ گیگ / ۳۰ روز", 250_000)]),
        ("حجمی اقتصادی", [("۵۰ گیگ / ۳۰ روز", 90_000)]),
    ):
        await create_draft_category(session, title_fa=group, kind="volume")
        await commit_and_activate(session, notes="گروه")
        view = await get_catalog_view(session)
        cat_id = next(c.id for c in view.categories if c.title_fa == group)
        for title, price in plans:
            await create_draft_package(
                session,
                category_id=cat_id,
                title_fa=title,
                fixed_price_toman=price,
                options={"volume_gb": 50, "duration_days": 30},
            )
            await commit_and_activate(session, notes="پلن")
    view = await get_catalog_view(session)
    for pkg in view.packages:
        cat = view.category(pkg.category_id)
        slugs[f"{cat.title_fa}|{pkg.title_fa}"] = pkg.slug
    return slugs


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(admin_telegram_ids=[ADMIN_ID], store_owner_telegram_ids=[ADMIN_ID])


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=ADMIN_ID, is_admin=True, is_reseller=False)


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=ADMIN_ID),
        answer=AsyncMock(),
        message=None,
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=ADMIN_ID),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`_tg_message` does an isinstance check a fake cannot satisfy."""
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr("bot.handlers.admin._tg_message", lambda _cb: msg)
    return msg


def _rendered(msg: SimpleNamespace) -> tuple[str, object]:
    """Body and markup of whatever was last put on screen."""
    call = msg.edit_text.await_args or msg.answer.await_args
    assert call is not None, "nothing was rendered"
    return call.args[0], call.kwargs.get("reply_markup")


def _labels(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
# Granting the role
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_granting_and_revoking_the_role(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    assert _role_label(shop) == texts.admin_customer_role_customer

    await admin_toggle_reseller(
        _callback(f"{AdminCB.CUST_RESELLER}{shop.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is True
    assert _role_label(shop) == texts.admin_customer_role_reseller

    await admin_toggle_reseller(
        _callback(f"{AdminCB.CUST_RESELLER}{shop.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is True, "the first tap on «حذف نمایندگی» only asks"
    await admin_revoke_reseller(
        _callback(f"{AdminCB.CUST_RESELLER_OK}{shop.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is False


@pytest.mark.asyncio
async def test_revoking_keeps_the_agreed_prices(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    """A shop that comes back must not have to be re-priced from scratch."""
    shop.is_reseller = True
    await set_plan_price(
        session, shop.id, "volume-plan-1", 110_000, duration_days=30, tier="economy"
    )
    await session.commit()

    await admin_toggle_reseller(
        _callback(f"{AdminCB.CUST_RESELLER}{shop.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is True, "the first tap on «حذف نمایندگی» only asks"
    await admin_revoke_reseller(
        _callback(f"{AdminCB.CUST_RESELLER_OK}{shop.id}"),  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is False
    assert (
        await get_plan_price(session, shop.id, "volume-plan-1", duration_days=30, tier="economy")
        == 110_000
    )


@pytest.mark.asyncio
async def test_a_non_admin_cannot_grant_the_role(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    callback = _callback(f"{AdminCB.CUST_RESELLER}{shop.id}")
    callback.from_user = SimpleNamespace(id=999)
    await admin_toggle_reseller(
        callback,  # type: ignore[arg-type]
        session,
        _settings(),  # type: ignore[arg-type]
        SimpleNamespace(id=2, telegram_id=999, is_admin=False, is_reseller=False),  # type: ignore[arg-type]
    )
    await session.refresh(shop)
    assert shop.is_reseller is False


# --------------------------------------------------------------------------- #
# The three sections
# --------------------------------------------------------------------------- #


async def _open_section(session: AsyncSession, shop: User, section: str) -> SimpleNamespace:
    state = _state()
    await admin_reseller_section(
        _callback(f"{AdminCB.RS_SEC}{shop.id}:{section}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    return state


@pytest.mark.asyncio
async def test_the_custom_rate_section_shows_only_its_two_numbers(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    """«هر بخش جدا» — the rate card is one decision, not a page of everything."""
    await _open_section(session, shop, "custom")
    body, markup = _rendered(screen)
    assert "نرخ خرید سفارشی" in body
    labels = _labels(markup)
    assert any("هر گیگ" in label for label in labels)
    assert any("هر روز" in label for label in labels)
    assert not any("سهمیه" in label for label in labels), "trials belong to their own screen"
    assert not any("گیگ / ۳۰ روز" in label for label in labels), "and so do the plans"


@pytest.mark.asyncio
async def test_the_trial_section_shows_only_trial_settings(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    await _open_section(session, shop, "trial")
    body, markup = _rendered(screen)
    assert "تست نمایندگی" in body
    labels = _labels(markup)
    assert any("سهمیهٔ ماهانه" in label for label in labels)
    assert any("تست اضافه" in label for label in labels)
    assert not any("هر گیگ" in label for label in labels)


@pytest.mark.asyncio
async def test_plans_matrix_lists_category_toggles(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    """T-007: categories become toggles; only the selected group's cells show."""
    await _open_section(session, shop, "plans")
    _, markup = _rendered(screen)
    labels = _labels(markup)
    cbs = _callbacks(markup)

    assert any(c.startswith(AdminCB.RS_CAT) for c in cbs)
    assert any(c.startswith(AdminCB.RS_DUR) for c in cbs)
    assert any("خرده" in lab for lab in labels)
    assert any(c.startswith(AdminCB.RS_CELL) for c in cbs)


@pytest.mark.asyncio
async def test_matrix_has_percent_and_undo_controls(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    """Preview/undo are the two guards on the percent op — both buttons must exist."""
    await _open_section(session, shop, "plans")
    _, markup = _rendered(screen)
    cbs = _callbacks(markup)
    assert AdminCB.RS_PCT in cbs
    assert AdminCB.RS_UNDO in cbs


@pytest.mark.asyncio
async def test_an_unpriced_plan_says_it_sells_at_retail(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    await _open_section(session, shop, "plans")
    _, markup = _rendered(screen)
    assert any("خرده" in label for label in _labels(markup))


@pytest.mark.asyncio
async def test_a_priced_plan_shows_the_agreed_amount_on_the_row(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    slug = catalog["حجمی ویژه|۵۰ گیگ / ۳۰ روز"]
    await set_plan_price(session, shop.id, slug, 110_000, duration_days=30, tier="economy")
    await session.commit()

    await _open_section(session, shop, "plans")
    _, markup = _rendered(screen)
    labels = _labels(markup)
    assert any("۱۱۰٬۰۰۰" in lab or "110" in lab for lab in labels)
    assert f"{AdminCB.RS_CELL}{slug}" in _callbacks(markup)


@pytest.mark.asyncio
async def test_an_unknown_section_does_not_dead_end(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    """A stale button from an older keyboard still has to land somewhere."""
    await _open_section(session, shop, "nonsense")
    _, markup = _rendered(screen)
    assert markup is not None and markup.inline_keyboard


# --------------------------------------------------------------------------- #
# Saving a number
# --------------------------------------------------------------------------- #


async def _save(session: AsyncSession, shop: User, field: str, text: str) -> SimpleNamespace:
    from bot.handlers.admin import admin_reseller_number_save

    state = _state()
    if field == "plan":
        raise AssertionError("use _save_plan")
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(rs_user_id=shop.id, rs_field=field)
    message = _typed(text)
    await admin_reseller_number_save(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    return message


@pytest.mark.asyncio
async def test_saving_each_rate_field(session: AsyncSession, shop: User) -> None:
    for field, value, attr in (
        ("gb", "1200", "price_per_gb_toman"),
        ("day", "500", "price_per_day_toman"),
        ("quota", "5", "trial_quota_monthly"),
        ("tprice", "5000", "trial_price_toman"),
    ):
        await _save(session, shop, field, value)
        rate = await get_rate(session, shop.id)
        assert getattr(rate, attr) == int(value), field


@pytest.mark.asyncio
async def test_persian_digits_are_accepted(session: AsyncSession, shop: User) -> None:
    """The owner types on a Persian keyboard; «۱۲۰۰» must not be «invalid»."""
    await _save(session, shop, "gb", "۱٬۲۰۰")
    rate = await get_rate(session, shop.id)
    assert rate.price_per_gb_toman == 1200


@pytest.mark.asyncio
async def test_a_rate_of_zero_is_allowed(session: AsyncSession, shop: User) -> None:
    """«این جزء رو حساب نکن» is a real answer for a per-day rate."""
    await _save(session, shop, "day", "0")
    rate = await get_rate(session, shop.id)
    assert rate.price_per_day_toman == 0


@pytest.mark.asyncio
async def test_junk_is_refused_without_leaking_python(session: AsyncSession, shop: User) -> None:
    shop_id = shop.id
    message = await _save(session, shop, "gb", "سلام")
    said = message.answer.await_args.args[0]
    assert "invalid literal" not in said
    assert await get_rate(session, shop_id) is None


@pytest.mark.asyncio
async def test_saving_a_rate_returns_to_its_own_section(session: AsyncSession, shop: User) -> None:
    """Setting three numbers in a row should not mean climbing out and back in."""
    message = await _save(session, shop, "gb", "1200")
    body = message.answer.await_args.args[0]
    assert "نرخ خرید سفارشی" in body

    message = await _save(session, shop, "quota", "5")
    body = message.answer.await_args.args[0]
    assert "تست نمایندگی" in body


@pytest.mark.asyncio
async def test_saving_a_plan_price_returns_to_the_plans_section(
    session: AsyncSession, shop: User, catalog: dict[str, str]
) -> None:
    from bot.handlers.admin import admin_reseller_number_save

    slug = catalog["حجمی ویژه|۵۰ گیگ / ۳۰ روز"]
    state = _state()
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(
        rs_user_id=shop.id,
        rs_field="plan",
        rs_slug=slug,
        rs_sel_duration_days=30,
        rs_sel_tier="economy",
    )
    message = _typed("110000")
    await admin_reseller_number_save(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await get_plan_price(session, shop.id, slug, duration_days=30, tier="economy") == 110_000
    body = (message.answer.await_args or message.edit_text.await_args).args[0]
    assert "قیمت‌های نمایندگی" in body


@pytest.mark.asyncio
async def test_a_free_plan_price_is_refused_in_persian(
    session: AsyncSession, shop: User, catalog: dict[str, str]
) -> None:
    from bot.handlers.admin import admin_reseller_number_save

    slug = catalog["حجمی ویژه|۵۰ گیگ / ۳۰ روز"]
    # Read the id **before** the handler runs. Rejecting the price rolls the
    # session back, which expires every loaded instance, and touching
    # `shop.id` afterwards needs a round-trip that raises MissingGreenlet in an
    # async session — the same trap CLAUDE.md flags for lazy relationships.
    shop_id = shop.id
    state = _state()
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(
        rs_user_id=shop_id,
        rs_field="plan",
        rs_slug=slug,
        rs_sel_duration_days=30,
        rs_sel_tier="economy",
    )
    message = _typed("0")
    await admin_reseller_number_save(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await get_plan_price(session, shop_id, slug, duration_days=30, tier="economy") is None


@pytest.mark.asyncio
async def test_clearing_a_plan_price(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    slug = catalog["حجمی ویژه|۵۰ گیگ / ۳۰ روز"]
    await set_plan_price(session, shop.id, slug, 110_000, duration_days=30, tier="economy")
    await session.commit()

    state = _state()
    await state.update_data(rs_user_id=shop.id, rs_sel_duration_days=30, rs_sel_tier="economy")
    await admin_reseller_plan_clear(
        _callback(f"{AdminCB.RS_PLAN_CLEAR}{shop.id}:{slug}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await list_plan_prices(session, shop.id, duration_days=30, tier="economy") == {}
    body, _ = _rendered(screen)
    assert "قیمت‌های نمایندگی" in body


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_rate_prompt_parks_the_flow_in_a_state(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    state = _state()
    await admin_reseller_rate_ask(
        _callback(f"{AdminCB.RS_RATE}{shop.id}:gb"),  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await state.get_state() == AdminStates.waiting_reseller_number.state
    data = await state.get_data()
    assert data["rs_user_id"] == shop.id
    assert data["rs_field"] == "gb"


@pytest.mark.asyncio
async def test_an_unknown_rate_field_is_refused(
    session: AsyncSession, shop: User, screen: SimpleNamespace
) -> None:
    state = _state()
    callback = _callback(f"{AdminCB.RS_RATE}{shop.id}:nonsense")
    await admin_reseller_rate_ask(
        callback,  # type: ignore[arg-type]
        state,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await state.get_state() is None
    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_the_plan_prompt_quotes_the_retail_price(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    """«این رو چند بدم؟» cannot be answered without knowing what it sells for."""
    slug = catalog["حجمی ویژه|۱۰۰ گیگ / ۳۰ روز"]
    state = _state()
    await admin_reseller_plan_ask(
        _callback(f"{AdminCB.RS_PLAN}{shop.id}:{slug}"),  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    body, _ = _rendered(screen)
    assert "۲۵۰٬۰۰۰" in body
    data = await state.get_data()
    assert data["rs_slug"] == slug


@pytest.mark.asyncio
async def test_a_plan_that_no_longer_exists_is_refused(
    session: AsyncSession, shop: User, screen: SimpleNamespace, catalog: dict[str, str]
) -> None:
    """Slugs outlive row ids, but a deleted plan still has to be handled."""
    state = _state()
    callback = _callback(f"{AdminCB.RS_PLAN}{shop.id}:gone-for-good")
    await admin_reseller_plan_ask(
        callback,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        _admin(),  # type: ignore[arg-type]
    )
    assert await state.get_state() is None
    assert callback.answer.await_args.kwargs.get("show_alert") is True


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_grouping_excludes_the_custom_template(
    session: AsyncSession, catalog: dict[str, str]
) -> None:
    """Only a fixed-price plan has a price a reseller price can override."""
    groups = await _ready_plans_by_group(session)
    assert [title for title, _ in groups] == ["حجمی ویژه", "حجمی اقتصادی"]
    assert all(
        p.fixed_price_toman for _, plans in groups for p in plans
    ), "a template with no price cannot carry a reseller price"


@pytest.mark.asyncio
async def test_an_empty_group_is_not_listed(session: AsyncSession) -> None:
    """A header with nothing under it is a row that says nothing."""
    await create_draft_category(session, title_fa="خالی", kind="volume")
    await commit_and_activate(session, notes="گروه")
    assert await _ready_plans_by_group(session) == []
