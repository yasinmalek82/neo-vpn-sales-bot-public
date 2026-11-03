"""Customer desk flow fixes (P1): one test per defect the owner's review found.

Each of these was a screen that worked but led the owner somewhere wrong: a ban
with no question, a Gregorian expiry, a back button to the wrong list, labels a
phone cuts. The one real data bug, `extend_days` leaving `Order.expires_at`
behind, is the reason renewal reminders fired on the old date.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client.exceptions import PasarGuardError
from bot.config import Settings
from bot.handlers import admin as ha
from bot.keyboards.admin import (
    AdminCB,
    customer_ban_confirm_keyboard,
    customer_card_keyboard,
    service_ops_keyboard,
    service_row_label,
    user_row_label,
)
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.plan import DataLimitResetStrategy
from services.customer_lookup import find_customers, get_customer_card
from services.service_ops import ServiceSnapshot, extend_days
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    set_category_trial,
)

ADMIN = 7
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    ADMIN_TELEGRAM_IDS=[ADMIN],
)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
GB = 1024**3


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _buttons(markup: Any) -> list[Any]:
    return [b for row in markup.inline_keyboard for b in row]


def _cbs(markup: Any) -> list[str]:
    return [b.callback_data for b in _buttons(markup)]


async def _user(session: AsyncSession, tg: int, **kw: Any) -> User:
    user = User(
        telegram_id=tg,
        username=kw.pop("username", f"u{tg}"),
        first_name=kw.pop("first_name", f"U{tg}"),
        language_code="fa",
        referral_code=f"R{tg}",
        **kw,
    )
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession) -> Plan:
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
    session.add(plan)
    await session.flush()
    return plan


async def _account(
    session: AsyncSession, user: User, username: str, n: int, **kw: Any
) -> PasarGuardAccount:
    account = PasarGuardAccount(
        user_id=user.id, panel_user_id=1000 + n, panel_username=username, **kw
    )
    session.add(account)
    await session.flush()
    return account


class FakePanel:
    """Per-username state; a name in ``broken`` raises like a timed-out panel."""

    def __init__(self, states: dict[str, dict[str, Any]] | None = None) -> None:
        self.states = states or {}
        self.broken: set[str] = set()
        self.disable_calls: list[tuple[str, bool]] = []

    def _state(self, username: str) -> dict[str, Any]:
        return self.states.setdefault(
            username, {"status": "active", "expire": None, "limit": None, "used": 0}
        )

    async def get_user(self, username: str) -> Any:
        if username in self.broken:
            raise PasarGuardError("panel down")
        st = self._state(username)
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value=st["status"]),
            expire=st["expire"],
            data_limit=st["limit"],
            used_traffic=st["used"],
        )

    async def disable_user(self, username: str, *, disabled: bool = True) -> Any:
        if username in self.broken:
            raise PasarGuardError("panel down")
        self.disable_calls.append((username, disabled))
        self._state(username)["status"] = "disabled" if disabled else "active"
        return await self.get_user(username)

    async def extend_subscription(self, username: str, *, expire: int, **_ignored: Any) -> Any:
        self._state(username)["expire"] = expire
        return await self.get_user(username)


def _patch_screen(monkeypatch: pytest.MonkeyPatch, panel: FakePanel | None = None) -> Any:
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)

    if panel is not None:

        @asynccontextmanager
        async def _fake_panel(_session: Any, _settings: Any) -> AsyncIterator[FakePanel]:
            yield panel

        monkeypatch.setattr(ha, "_panel", _fake_panel)
    return screen


def _call(data: str) -> Any:
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=ADMIN), answer=AsyncMock())


def _shown_keyboard(screen: Any) -> Any:
    return screen.edit_text.await_args.kwargs["reply_markup"]


def _shown_text(screen: Any) -> str:
    return screen.edit_text.await_args.args[0]


# --------------------------------------------------------------------------- #
# 1. Ban from the card: asks first, chooses «فقط ربات» or «+ سرویس‌ها»,
#    lands back on the card, and one failing service never stops the rest
# --------------------------------------------------------------------------- #


def test_ban_callbacks_are_short_and_do_not_shadow_another_prefix() -> None:
    assert len(f"{AdminCB.CUST_BAN_OK}{2**40}:us".encode()) <= 64
    others = [
        v
        for k, v in vars(AdminCB).items()
        if isinstance(v, str) and not k.startswith("_") and k not in {"CUST_BAN", "CUST_BAN_OK"}
    ]
    for new in (AdminCB.CUST_BAN, AdminCB.CUST_BAN_OK):
        assert not any(o.startswith(new) or new.startswith(o) for o in others), new
    assert not AdminCB.CUST_BAN_OK.startswith(AdminCB.CUST_BAN)


def test_card_ban_switch_asks_and_confirm_offers_both_scopes() -> None:
    card = customer_card_keyboard(9, banned=False)
    assert f"{AdminCB.CUST_BAN}9" in _cbs(card)
    assert not any(c.startswith(AdminCB.BAN_FROM_ORDER) for c in _cbs(card))

    ask = customer_ban_confirm_keyboard(9, unban=False, services_count=3)
    assert [b.callback_data for b in _buttons(ask)][:2] == [
        f"{AdminCB.CUST_BAN_OK}9:b",
        f"{AdminCB.CUST_BAN_OK}9:s",
    ]
    assert all(len(row) == 1 for row in ask.inline_keyboard), "destructive buttons alone"
    only_bot = customer_ban_confirm_keyboard(9, unban=False, services_count=0)
    assert f"{AdminCB.CUST_BAN_OK}9:s" not in _cbs(only_bot)
    back = customer_ban_confirm_keyboard(9, unban=True, services_count=2)
    assert {f"{AdminCB.CUST_BAN_OK}9:ub", f"{AdminCB.CUST_BAN_OK}9:us"} <= set(_cbs(back))


@pytest.mark.asyncio
async def test_ban_with_services_cuts_all_but_counts_the_one_that_failed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session, 500)
    for i, name in enumerate(("a_1", "b_2", "c_3")):
        await _account(session, user, name, i)
    await session.commit()
    panel = FakePanel()
    panel.broken.add("b_2")
    screen = _patch_screen(monkeypatch, panel)

    call = _call(f"{AdminCB.CUST_BAN_OK}{user.id}:s")
    await ha.admin_customer_ban_apply(call, session, SETTINGS, None)

    assert (await session.get(User, user.id)).is_banned is True  # type: ignore[union-attr]
    assert sorted(panel.disable_calls) == [("a_1", True), ("c_3", True)], "went past the failure"
    note = call.answer.await_args.args[0]
    assert "۲" in note and "۱" in note, note
    assert screen.edit_text.await_args is not None, "back on the card, not a dead end"
    assert _shown_text(screen).startswith("👤"), "the customer card"


@pytest.mark.asyncio
async def test_ban_bot_only_touches_no_service_and_unban_restores(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session, 501)
    await _account(session, user, "a_1", 1)
    await session.commit()
    panel = FakePanel()
    _patch_screen(monkeypatch, panel)

    await ha.admin_customer_ban_apply(
        _call(f"{AdminCB.CUST_BAN_OK}{user.id}:b"), session, SETTINGS, None
    )
    assert panel.disable_calls == []
    assert (await session.get(User, user.id)).is_banned is True  # type: ignore[union-attr]

    await ha.admin_customer_ban_apply(
        _call(f"{AdminCB.CUST_BAN_OK}{user.id}:us"), session, SETTINGS, None
    )
    assert panel.disable_calls == [("a_1", False)]
    assert (await session.get(User, user.id)).is_banned is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_an_admin_cannot_be_banned_from_the_card(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    boss_id = (await _user(session, ADMIN)).id
    await session.commit()
    _patch_screen(monkeypatch, FakePanel())
    call = _call(f"{AdminCB.CUST_BAN_OK}{boss_id}:b")
    await ha.admin_customer_ban_apply(call, session, SETTINGS, None)
    assert (await session.get(User, boss_id)).is_banned is False  # type: ignore[union-attr]
    assert call.answer.await_args.kwargs.get("show_alert") is True


# --------------------------------------------------------------------------- #
# 2. Expiry is Jalali, with the days left
# --------------------------------------------------------------------------- #


def _snap(expire_at: datetime | None, **kw: Any) -> ServiceSnapshot:
    return ServiceSnapshot(
        panel_username="x",
        status=kw.pop("status", "active"),
        expire_unix=int(expire_at.timestamp()) if expire_at else None,
        data_limit_bytes=kw.pop("limit", None),
        used_traffic_bytes=kw.pop("used", 0),
    )


def test_expiry_reads_jalali_with_days_left_and_past_says_expired() -> None:
    future = ha._fmt_expire(_snap(datetime.now(UTC) + timedelta(days=12, hours=2)))
    assert re.fullmatch(r"۱۴\d\d/\d\d/\d\d \(۱۲ روز مانده\)", future), future
    past = ha._fmt_expire(_snap(datetime.now(UTC) - timedelta(days=3)))
    assert re.match(r"۱۴\d\d/\d\d/\d\d", past) and "منقضی" in past, past
    assert "2026" not in future + past and "-" not in future.split(" ")[0]
    assert ha._fmt_expire(_snap(None)) == "بدون انقضا"


# --------------------------------------------------------------------------- #
# 3. Service card: back to the list, and a toggle that reads state + action
# --------------------------------------------------------------------------- #


def test_service_card_goes_back_to_the_service_list_and_toggle_reads_state() -> None:
    live = service_ops_keyboard(5, 9, disabled=False)
    cut = service_ops_keyboard(5, 9, disabled=True)
    assert f"{AdminCB.CUST_SERVICES}9" in _cbs(live)
    assert f"{AdminCB.CUSTOMER_PICK}9" in _cbs(live)
    assert "⛔️ قطع سرویس" in [b.text for b in _buttons(live)]
    assert "✅ فعال‌سازی سرویس" in [b.text for b in _buttons(cut)]
    toggle = next(b for b in _buttons(live) if b.callback_data.startswith(AdminCB.SVC_TOGGLE))
    assert toggle.callback_data == f"{AdminCB.SVC_TOGGLE}5", "the confirm step stays"


# --------------------------------------------------------------------------- #
# 4. Referrals are buttons, paged by 8; the card links to the referrer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_referrals_are_paged_buttons_and_the_card_links_the_referrer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    boss = await _user(session, 600)
    kids = [await _user(session, 610 + i, referred_by_id=boss.id) for i in range(10)]
    await session.commit()
    screen = _patch_screen(monkeypatch)

    await ha.admin_customer_referrals(
        _call(f"{AdminCB.CUST_REFS}{boss.id}"), session, SETTINGS, None
    )
    first = _cbs(_shown_keyboard(screen))
    picks = [
        c
        for c in first
        if c.startswith(AdminCB.CUSTOMER_PICK) and c != f"{AdminCB.CUSTOMER_PICK}{boss.id}"
    ]
    assert len(picks) == 8
    assert f"{AdminCB.CUST_REFS}{boss.id}:8" in first
    await ha.admin_customer_referrals(
        _call(f"{AdminCB.CUST_REFS}{boss.id}:8"), session, SETTINGS, None
    )
    second = _cbs(_shown_keyboard(screen))
    assert len([c for c in second if c.startswith(AdminCB.CUSTOMER_PICK)]) == 2 + 1  # + card back
    assert f"{AdminCB.CUST_REFS}{boss.id}:0" in second

    with_ref = customer_card_keyboard(kids[0].id, banned=False, referrer_id=boss.id)
    assert f"{AdminCB.CUSTOMER_PICK}{boss.id}" in _cbs(with_ref)
    assert "👤 معرف" in [b.text for b in _buttons(with_ref)]
    assert "👤 معرف" not in [b.text for b in _buttons(customer_card_keyboard(1, banned=False))]


# --------------------------------------------------------------------------- #
# 5. User list labels: name first, numbers never cut, ≤ 40
# --------------------------------------------------------------------------- #


def _row(label: str, wallet: int = 0, services: int = 0, **kw: Any) -> Any:
    return SimpleNamespace(
        label=label,
        wallet_balance_toman=wallet,
        service_count=services,
        is_banned=kw.get("banned", False),
        is_admin=kw.get("admin", False),
    )


def test_user_row_label_shape_units_and_length() -> None:
    assert user_row_label(_row("رضا", 12_000, 2)) == "رضا • 🏦 ۱۲ هزار • 📱 ۲"
    assert user_row_label(_row("رضا", 12_345)) == "رضا • 🏦 ۱۲٬۳۴۵"
    assert user_row_label(_row("رضا")) == "رضا", "no wallet, no services: no zeros"
    assert user_row_label(_row("رضا", 0, 3, banned=True)).startswith("🚫رضا")
    worst = user_row_label(_row("ن" * 60, 1_234_567_890, 12, banned=True, admin=True))
    assert len(worst) <= 40 and worst.endswith("📱 ۱۲") and "۱٬۲۳۴٬۵۶۷٬۸۹۰" in worst, worst
    long_name = user_row_label(_row("ن" * 60, 5_000, 1))
    assert long_name.split(" • ")[0].endswith("…") and len(long_name.split(" • ")[0]) <= 18


# --------------------------------------------------------------------------- #
# 6. Services list with live status, paged by 8
# --------------------------------------------------------------------------- #


def test_service_row_label_states() -> None:
    soon = datetime.now(UTC) + timedelta(days=12, hours=2)
    ok = service_row_label("رضا", _snap(soon, limit=10 * GB, used=int(6.5 * GB)))
    assert ok == "🟢 رضا • ۱۲ روز • ۳٫۵ گیگ", ok
    assert service_row_label("رضا", _snap(soon - timedelta(days=30))) == "🔴 رضا • منقضی"
    assert service_row_label("رضا", _snap(soon, status="disabled")) == "⛔ رضا • قطع"
    assert service_row_label("رضا", None) == "⚪️ رضا"
    long = service_row_label("ن" * 70, _snap(soon, limit=10 * GB, used=int(6.5 * GB)))
    assert len(long) <= 40 and long.endswith("۱۲ روز • ۳٫۵ گیگ"), long


@pytest.mark.asyncio
async def test_service_list_shows_live_status_and_survives_a_dead_panel(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session, 700)
    soon = int((datetime.now(UTC) + timedelta(days=5, hours=2)).timestamp())
    for i in range(10):
        await _account(session, user, f"s{i}", i, display_name=f"svc{i}")
    await session.commit()
    panel = FakePanel({"s9": {"status": "disabled", "expire": soon, "limit": None, "used": 0}})
    panel.states["s8"] = {"status": "active", "expire": soon, "limit": None, "used": 0}
    panel.broken.add("s7")
    screen = _patch_screen(monkeypatch, panel)

    await ha.admin_customer_services(
        _call(f"{AdminCB.CUST_SERVICES}{user.id}"), session, SETTINGS, None
    )
    kb = _shown_keyboard(screen)
    rows = [b.text for b in _buttons(kb) if b.callback_data.startswith(AdminCB.SVC)]
    assert len(rows) == 8, "paged by 8"
    assert f"{AdminCB.CUST_SERVICES}{user.id}:8" in _cbs(kb)
    assert rows[0] == "⛔ svc9 • قطع"
    assert rows[1].startswith("🟢 svc8 • ۵ روز"), rows
    assert rows[2].startswith("⚪️ svc7"), "a failed read is unknown, not an error"
    assert all(len(t) <= 40 for t in rows)

    # With no panel client at all, every row is unknown but the screen opens.
    @asynccontextmanager
    async def no_panel(_session: Any, _settings: Any) -> AsyncIterator[Any]:
        raise RuntimeError("no panel configured")
        yield  # pragma: no cover

    monkeypatch.setattr(ha, "_panel", no_panel)
    await ha.admin_customer_services(
        _call(f"{AdminCB.CUST_SERVICES}{user.id}"), session, SETTINGS, None
    )
    assert all(
        b.text.startswith("⚪️")
        for b in _buttons(_shown_keyboard(screen))
        if b.callback_data.startswith(AdminCB.SVC)
    )


# --------------------------------------------------------------------------- #
# 7. Card text: a trial line per group, services as a count only
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_card_shows_trial_per_group_and_only_a_service_count(session: AsyncSession) -> None:
    volume = await create_draft_category(session, title_fa="ویژه", kind="volume")
    direct = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await create_draft_category(session, title_fa="بدون تست", kind="volume")
    await set_category_trial(session, volume.id, data_limit_gb=1, duration_days=1)
    await set_category_trial(session, direct.id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="seed")

    user = await _user(session, 800)
    plan = await _plan(session)
    when = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    session.add(
        Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=0,
            discount_toman=0,
            force_new=True,
            trial_kind=TRIAL_KIND_PERSONAL,
            trial_category_slug=str(volume.slug),
            created_at=when,
        )
    )
    await _account(session, user, "svc_one", 1, display_name="سرویس اول")
    await session.commit()

    text = ha._card_text(await get_customer_card(session, user.id))
    assert re.search(r"تست: ویژه ✓ ۱۴۰۵/۰۷/۰۸ • نامحدود —", text), text
    assert "بدون تست" not in text, "a group without a trial is not listed"
    assert "سرویس اول" not in text and "svc_one" not in text
    assert "سرویس‌ها: ۱" in text

    # A reset (voided) trial no longer counts.
    for order in (await session.scalars(select(Order))).all():
        order.trial_voided_at = when
    await session.commit()
    text = ha._card_text(await get_customer_card(session, user.id))
    assert "ویژه —" in text and "✓" not in text


@pytest.mark.asyncio
async def test_a_trial_from_before_groups_counts_on_the_unlimited_group(
    session: AsyncSession,
) -> None:
    volume = await create_draft_category(session, title_fa="ویژه", kind="volume")
    direct = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await set_category_trial(session, volume.id, data_limit_gb=1, duration_days=1)
    await set_category_trial(session, direct.id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="seed")
    user = await _user(session, 801, trial_claimed_at=datetime(2026, 9, 1, tzinfo=UTC))
    await session.commit()  # trial_claimed_at and no trial order: an import
    text = ha._card_text(await get_customer_card(session, user.id))
    assert "نامحدود ✓" in text and "ویژه —" in text, text


# --------------------------------------------------------------------------- #
# 8. Search: config name, subscription link, order number
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_finds_by_config_name_link_and_order_number(session: AsyncSession) -> None:
    alice = await _user(session, 900, username="alice", first_name="Alice")
    bob = await _user(session, 901, username="bob", first_name="Bob")
    await _account(
        session,
        alice,
        "reza_a7f3",
        1,
        display_name="Reza Shop",
        subscription_url="https://panel.example.com/sub/TOKEN1234abcd",
    )
    await _account(session, bob, "other_zz", 2, service_name="Mahdi")
    plan = await _plan(session)
    order = Order(
        user_id=bob.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=0,
        discount_toman=0,
        force_new=True,
    )
    session.add(order)
    await session.commit()

    assert [u.id for u in await find_customers(session, "reza_a7f3")] == [alice.id]
    assert [u.id for u in await find_customers(session, "shop")] == [alice.id]
    assert [u.id for u in await find_customers(session, "mahdi")] == [bob.id]
    link = "https://panel.example.com/sub/TOKEN1234abcd?x=1"
    assert [u.id for u in await find_customers(session, link)] == [alice.id]
    assert [u.id for u in await find_customers(session, f"#{order.id}")] == [bob.id]
    persian = "".join("۰۱۲۳۴۵۶۷۸۹"[int(c)] for c in str(order.id))
    assert [u.id for u in await find_customers(session, f"سفارش {persian}")] == [bob.id]
    assert await find_customers(session, "#99999") == []


@pytest.mark.asyncio
async def test_search_returns_distinct_users_capped_at_eight(session: AsyncSession) -> None:
    for i in range(10):
        user = await _user(session, 950 + i, username=f"shop{i}", first_name="X")
        await _account(session, user, f"shop_a{i}", i)
        await _account(session, user, f"shop_b{i}", 100 + i)
    await session.commit()
    found = await find_customers(session, "shop")
    assert len(found) == 8 and len({u.id for u in found}) == 8
    assert (
        "#123" in ha.texts.admin_customer_prompt and "reza_a7f3" in ha.texts.admin_customer_prompt
    )


# --------------------------------------------------------------------------- #
# 9. A manual extension moves Order.expires_at (renewal reminders read it)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extend_days_moves_the_orders_expiry_reminders_read(session: AsyncSession) -> None:
    user = await _user(session, 990)
    plan = await _plan(session)
    account = await _account(session, user, "yasin_ab12", 1)
    old_expiry = NOW + timedelta(days=3)

    def order(username: str, status: OrderStatus, expires: datetime) -> Order:
        return Order(
            user_id=user.id,
            plan_id=plan.id,
            status=status,
            price_toman=0,
            discount_toman=0,
            force_new=True,
            panel_username=username,
            expires_at=expires,
        )

    mine = order("yasin_ab12", OrderStatus.ACTIVE, old_expiry)
    already_later = order("yasin_ab12", OrderStatus.ACTIVE, NOW + timedelta(days=400))
    dead = order("yasin_ab12", OrderStatus.EXPIRED, old_expiry)
    someone_else = order("other_zz", OrderStatus.ACTIVE, old_expiry)
    session.add_all([mine, already_later, dead, someone_else])
    await session.commit()

    panel = FakePanel(
        {
            "yasin_ab12": {
                "status": "active",
                "expire": int(old_expiry.timestamp()),
                "limit": None,
                "used": 0,
            }
        }
    )
    await extend_days(session, panel, account_id=account.id, days=7, now=NOW)  # type: ignore[arg-type]

    new_expiry = old_expiry + timedelta(days=7)

    def utc(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    await session.refresh(mine)
    assert utc(mine.expires_at) == new_expiry  # type: ignore[arg-type]
    for untouched, expected in (
        (already_later, NOW + timedelta(days=400)),
        (dead, old_expiry),
        (someone_else, old_expiry),
    ):
        await session.refresh(untouched)
        assert utc(untouched.expires_at) == expected  # type: ignore[arg-type]


async def test_expiring_filter_lists_only_active_orders_inside_the_window(
    session: AsyncSession,
) -> None:
    from services.customer_lookup import UserFilter, list_users

    plan = await _plan(session)
    now = datetime.now(UTC)
    cases = {
        "inside": (OrderStatus.ACTIVE, now + timedelta(days=3)),
        "edge": (OrderStatus.ACTIVE, now + timedelta(days=7, seconds=-30)),
        "beyond": (OrderStatus.ACTIVE, now + timedelta(days=8)),
        "expired": (OrderStatus.ACTIVE, now - timedelta(days=1)),
        "rejected": (OrderStatus.REJECTED, now + timedelta(days=3)),
    }
    ids: dict[str, int] = {}
    for n, (name, (status, when)) in enumerate(cases.items(), start=9100):
        user = await _user(session, n)
        ids[name] = user.id
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=status,
                price_toman=0,
                discount_toman=0,
                force_new=True,
                expires_at=when,
            )
        )
    await session.commit()

    page = await list_users(session, user_filter=UserFilter.EXPIRING)
    assert page.total == 2
    assert {r.user_id for r in page.rows} == {ids["inside"], ids["edge"]}
