"""Admin home, customer card and resellers list, rearranged (owner, 2026-09-30).

Simpler by arrangement, never by removing an option: every button the old
home had must still be reachable, from exactly one place.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.admin_home import home_text
from bot.keyboards.admin import (
    AdminCB,
    admin_home_keyboard,
    admin_people_keyboard,
    admin_resellers_keyboard,
    admin_sales_keyboard,
    admin_settings_keyboard,
    customer_card_keyboard,
)
from bot.keyboards.style import DANGER, PRIMARY
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.admin_home import PanelDown, get_home_status
from services.reporting import FinanceWindow
from services.store_admin import commit_and_activate, create_draft_category, update_draft_category
from services.trial import reseller_month_sales

OLD_HOME = {
    AdminCB.PENDING,
    AdminCB.ORDERS,
    AdminCB.LOOKUP,
    AdminCB.STATS,
    AdminCB.CUSTOMER,
    AdminCB.RESELLERS,
    AdminCB.STORE,
    AdminCB.BAN,
    AdminCB.UNBAN,
    AdminCB.PANEL,
    AdminCB.REPORTS,
    AdminCB.CAST,
    AdminCB.QR_BG,
}


def _buttons(markup: Any) -> list[Any]:
    return [b for row in markup.inline_keyboard for b in row]


def _cbs(markup: Any) -> list[str]:
    return [b.callback_data for b in _buttons(markup)]


def test_every_old_home_button_is_reachable_from_exactly_one_place() -> None:
    screens = [
        admin_home_keyboard(),
        admin_sales_keyboard(),
        admin_people_keyboard(),
        admin_settings_keyboard(),
    ]
    found = [cb for kb in screens for cb in _cbs(kb) if cb in OLD_HOME]
    assert sorted(found) == sorted(OLD_HOME)
    home = set(_cbs(admin_home_keyboard()))
    assert {AdminCB.SEC_SALES, AdminCB.SEC_PEOPLE, AdminCB.SEC_SETTINGS} <= home


def test_payments_are_coloured_only_when_something_waits() -> None:
    idle = admin_home_keyboard(pending_count=0)
    busy = admin_home_keyboard(pending_count=3)
    assert idle.inline_keyboard[0][0].style is None
    top = busy.inline_keyboard[0]
    assert len(top) == 1 and top[0].style == PRIMARY and "۳" in top[0].text


def _status(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "pending": 0,
        "stale": 0,
        "today": FinanceWindow(label_fa="امروز", orders=2, revenue_toman=150_000),
        "groups_without_panel": [],
        "report_attached": True,
        "report_unready": 0,
        "panel_down": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_home_text_warns_only_about_what_is_wrong() -> None:
    calm = home_text(_status())  # type: ignore[arg-type]
    assert "⚠️" not in calm
    assert "۱۵۰٬۰۰۰" in calm and "۲ فروش" in calm
    busy = home_text(
        _status(pending=3, stale=1, groups_without_panel=["ویژه"], report_unready=2)  # type: ignore[arg-type]
    )
    assert "۳ پرداخت" in busy and "۱ کهنه" in busy
    assert "«ویژه» گروه پنل ندارد" in busy
    assert "۲ تاپیک" in busy
    assert "وصل نیست" in home_text(_status(report_attached=False))  # type: ignore[arg-type]


def test_home_warns_when_a_node_or_the_panel_is_down() -> None:
    since = datetime.now(UTC) - timedelta(minutes=12)
    calm = home_text(_status())  # type: ignore[arg-type]
    assert "قطع است" not in calm and "در دسترس نیست" not in calm
    node = home_text(_status(panel_down=[PanelDown(name="tunnel-b", since=since)]))  # type: ignore[arg-type]
    assert "⚠️ سرور «tunnel-b» قطع است (از ۱۲ دقیقه قبل)" in node
    both = home_text(
        _status(  # type: ignore[arg-type]
            panel_down=[
                PanelDown(name="tunnel-b", since=since),
                PanelDown(name="panel", since=since, is_panel=True),
            ]
        )
    )
    assert "⚠️ پنل در دسترس نیست (از ۱۲ دقیقه قبل)" in both
    assert both.index("پنل در دسترس نیست") < both.index("tunnel-b")
    hostile = home_text(_status(panel_down=[PanelDown(name="<b>x</b>", since=since)]))  # type: ignore[arg-type]
    assert "<b>x</b>" not in hostile


def test_customer_card_switches_read_state_and_action() -> None:
    plain = customer_card_keyboard(9, banned=False)
    labels = [b.text for b in _buttons(plain)]
    assert "🚫 مسدود کردن" in labels
    assert "🏪 نماینده کردن" in labels
    assert not any("قیمت‌های نمایندگی" in t for t in labels)
    assert not any("تست دوباره" in t for t in labels)
    assert not any(c.startswith(AdminCB.WALLET_ZERO) for c in _cbs(plain)), "nothing to zero"

    full = customer_card_keyboard(
        9, banned=True, reseller=True, trial_taken=True, wallet_toman=5_000
    )
    labels = [b.text for b in _buttons(full)]
    assert "✅ رفع مسدودی" in labels
    assert "🏪 حذف نمایندگی" in labels
    assert any("قیمت‌های نمایندگی" in t for t in labels)
    assert any("تست دوباره" in t for t in labels)
    zero = [
        row for row in full.inline_keyboard if row[0].callback_data.startswith(AdminCB.WALLET_ZERO)
    ]
    assert len(zero) == 1 and len(zero[0]) == 1 and zero[0][0].style == DANGER


def test_resellers_list_shows_the_month() -> None:
    shops = [
        SimpleNamespace(id=1, first_name="رضا موبایل", username=None, telegram_id=1),
        SimpleNamespace(id=2, first_name="علی", username=None, telegram_id=2),
    ]
    labels = [
        b.text for b in _buttons(admin_resellers_keyboard(shops, {1: (12, 3, 10), 2: (0, 0, 0)}))
    ]
    assert "رضا موبایل • ۱۲ فروش • تست ۳/۱۰" in labels
    assert "علی • ۰ فروش" in labels, "no quota and no trials: no «تست ۰/۰»"


# --------------------------------------------------------------------------- #
# Database-backed
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user_and_plan(session: AsyncSession, tg: int = 500) -> tuple[User, Plan]:
    user = User(
        telegram_id=tg, username="u", first_name="U", language_code="fa", referral_code=f"R{tg}"
    )
    plan = Plan(
        name="p",
        description="p",
        data_limit_gb=5,
        duration_days=30,
        price_toman=0,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.flush()
    return user, plan


def _order(user: User, plan: Plan, **kw: Any) -> Order:
    return Order(user_id=user.id, plan_id=plan.id, discount_toman=0, force_new=True, **kw)


@pytest.mark.asyncio
async def test_home_status_counts_and_flags(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    user, plan = await _user_and_plan(session)
    other, _ = await _user_and_plan(session, tg=501)  # one open order per user
    session.add_all(
        [
            _order(
                other,
                plan,
                status=OrderStatus.PAYMENT_REVIEW,
                price_toman=100,
                created_at=now - timedelta(hours=3),
            ),
            _order(user, plan, status=OrderStatus.PAYMENT_REVIEW, price_toman=100, created_at=now),
            _order(user, plan, status=OrderStatus.ACTIVE, price_toman=90_000, activated_at=now),
            _order(user, plan, status=OrderStatus.ACTIVE, price_toman=0, activated_at=now),
        ]
    )
    bare = await create_draft_category(session, title_fa="ویژه", kind="volume")
    hidden = await create_draft_category(session, title_fa="خاموش", kind="volume")
    ready = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await update_draft_category(session, hidden.id, enabled=False)
    await update_draft_category(session, ready.id, panel_group_ids=[3])
    await commit_and_activate(session, notes="x")

    status = await get_home_status(session, now=now)
    assert (status.pending, status.stale) == (2, 1)
    assert (status.today.orders, status.today.revenue_toman) == (
        1,
        90_000,
    ), "a giveaway is not a sale"
    assert status.groups_without_panel == [bare.title_fa]
    assert status.report_attached is False


@pytest.mark.asyncio
async def test_reseller_month_sales_counts_money_this_month_only(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    user, plan = await _user_and_plan(session)
    session.add_all(
        [
            _order(user, plan, status=OrderStatus.ACTIVE, price_toman=50_000, activated_at=now),
            _order(user, plan, status=OrderStatus.ACTIVE, price_toman=0, activated_at=now),
            _order(
                user,
                plan,
                status=OrderStatus.ACTIVE,
                price_toman=50_000,
                activated_at=now - timedelta(days=40),
            ),
            _order(user, plan, status=OrderStatus.PAYMENT_REVIEW, price_toman=50_000),
        ]
    )
    await session.flush()
    assert await reseller_month_sales(session, user.id, now=now) == 1


@pytest.mark.asyncio
async def test_section_buttons_open_their_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.config import Settings
    from bot.handlers import admin as ha

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS=[7],
    )
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))
    for cb, expect in (
        (AdminCB.SEC_SALES, AdminCB.ORDERS),
        (AdminCB.SEC_PEOPLE, AdminCB.RESELLERS),
        (AdminCB.SEC_SETTINGS, AdminCB.PANEL),
    ):
        call = SimpleNamespace(
            data=cb, from_user=SimpleNamespace(id=7), answer=AsyncMock(), message=screen
        )
        await ha.admin_home_section(call, state, settings, None)  # type: ignore[arg-type]
        assert expect in _cbs(screen.edit_text.await_args.kwargs["reply_markup"])


# --------------------------------------------------------------------------- #
# Customer card: services and orders as lists of buttons (owner, 2026-09-30)
# --------------------------------------------------------------------------- #


def test_card_has_one_button_each_for_services_and_orders() -> None:
    from bot.keyboards.admin import customer_card_keyboard as card

    svcs = [SimpleNamespace(display_name=f"s{i}", account_id=i) for i in range(5)]
    markup = card(9, banned=False, services=svcs, orders_total=12)
    cbs = _cbs(markup)
    assert not any(c.startswith(AdminCB.SVC) for c in cbs), "no row per service on the card"
    labels = [b.text for b in _buttons(markup)]
    assert "📱 سرویس‌ها (۵)" in labels and "🛍 سفارش‌ها (۱۲)" in labels
    empty = _cbs(card(9, banned=False))
    assert not any(c.startswith((AdminCB.CUST_SERVICES, AdminCB.CUST_ORDERS)) for c in empty)


def test_order_label_is_short_and_back_returns_to_the_customer_list() -> None:
    from bot.keyboards.admin import SRC_CUSTOMER, customer_order_label, order_cb, parse_order_cb

    order = SimpleNamespace(
        id=12345, price_toman=1_250_000, created_at=datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
    )
    from bot.handlers.admin import _ORDER_STATUS_SHORT_FA

    longest = max(_ORDER_STATUS_SHORT_FA.values(), key=len)
    label = customer_order_label(order, longest)
    assert len(label) <= 40 and ":" not in label, label
    assert customer_order_label(SimpleNamespace(id=1, price_toman=0, created_at=None), "فعال") == (
        "#۱ • فعال • رایگان • —"
    )
    cb = order_cb(55, src=SRC_CUSTOMER, key="9", offset=8)
    assert len(cb.encode()) <= 64
    assert parse_order_cb(cb) == (55, f"{AdminCB.CUST_ORDERS}9:8")


@pytest.mark.asyncio
async def test_customer_orders_are_paged_buttons_that_open_the_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot.config import Settings
    from bot.handlers import admin as ha

    user, plan = await _user_and_plan(session)
    for i in range(10):
        session.add(_order(user, plan, status=OrderStatus.ACTIVE, price_toman=1000 * (i + 1)))
    await session.flush()
    settings = Settings(_env_file=None, BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS=[7])  # type: ignore[call-arg]
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)

    async def open_page(data: str) -> list[str]:
        call = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=7), answer=AsyncMock(), message=screen
        )
        await ha.admin_customer_orders(call, session, settings, None)  # type: ignore[arg-type]
        return _cbs(screen.edit_text.await_args.kwargs["reply_markup"])

    first = await open_page(f"{AdminCB.CUST_ORDERS}{user.id}")
    opens = [c for c in first if c.startswith(AdminCB.ORDER)]
    assert len(opens) == 8
    assert f"{AdminCB.CUST_ORDERS}{user.id}:8" in first, "next page"
    second = await open_page(f"{AdminCB.CUST_ORDERS}{user.id}:8")
    assert len([c for c in second if c.startswith(AdminCB.ORDER)]) == 2
    assert f"{AdminCB.CUST_ORDERS}{user.id}:0" in second, "previous page"
    assert "۱۰" in screen.edit_text.await_args.args[0]


# --- «🌐 پنل وب»: a new entry, only while the Mini App panel is on -------------

PANEL_URL = "https://panel.example.com/panel/"


def _web_buttons(markup: Any) -> list[Any]:
    return [b for b in _buttons(markup) if b.web_app is not None]


def test_the_web_button_is_absent_while_the_panel_is_off() -> None:
    assert _web_buttons(admin_home_keyboard()) == []
    assert _web_buttons(admin_home_keyboard(web_panel_url="")) == []


def test_the_web_button_opens_the_panel_and_takes_nothing_from_the_old_home() -> None:
    plain = admin_home_keyboard(pending_count=2)
    withweb = admin_home_keyboard(pending_count=2, web_panel_url=PANEL_URL)
    web = _web_buttons(withweb)
    assert [b.web_app.url for b in web] == [PANEL_URL]
    assert web[0].callback_data is None and web[0].style is None, "never coloured, no callback"
    # Every old entry is still there, in the same place; the new one pairs with settings.
    assert [b.callback_data for b in _buttons(withweb) if b.web_app is None] == _cbs(plain)
    assert withweb.inline_keyboard[:-1] == plain.inline_keyboard[:-1]
    assert [b.callback_data for b in withweb.inline_keyboard[-1]] == [AdminCB.SEC_SETTINGS, None]
    # Still one accent on the screen: the payments button.
    assert [b.style for b in _buttons(withweb) if b.style] == [PRIMARY]


@pytest.mark.asyncio
async def test_the_home_screen_passes_the_link_to_the_keyboard(session: AsyncSession) -> None:
    from bot.admin_home import admin_home_screen

    _, off = await admin_home_screen(session)
    _, on = await admin_home_screen(session, PANEL_URL)
    assert _web_buttons(off) == []
    assert [b.web_app.url for b in _web_buttons(on)] == [PANEL_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_every_way_home_follows_the_panel_switch(
    session: AsyncSession, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/admin, the reply-keyboard button and every «🏠» all render the same home."""
    from aiogram.fsm.context import FSMContext

    from bot.config import Settings
    from bot.handlers import admin as admin_handlers
    from bot.handlers import common, reply_menu

    settings = Settings(
        BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="42",
        WEB_PANEL_ENABLED=enabled,
        WEB_PANEL_URL=PANEL_URL,
    )
    expected = [PANEL_URL] if enabled else []

    def urls(markup: Any) -> list[str]:
        return [b.web_app.url for b in _web_buttons(markup)]

    state = AsyncMock(spec=FSMContext)
    for entry in (common.cmd_admin, reply_menu.reply_admin):
        message = AsyncMock()
        message.from_user = SimpleNamespace(id=42)
        await entry(message, state, session, settings, None)
        assert urls(message.answer.await_args.kwargs["reply_markup"]) == expected

    sent: list[Any] = []

    async def fake_edit_or_send(_message: Any, _body: str, markup: Any) -> None:
        sent.append(markup)

    monkeypatch.setattr(admin_handlers, "_tg_message", lambda _cb: AsyncMock())
    monkeypatch.setattr(admin_handlers, "edit_or_send", fake_edit_or_send)
    await admin_handlers._show_home(AsyncMock(), state, session, settings)
    assert [urls(m) for m in sent] == [expected]
