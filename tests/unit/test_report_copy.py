"""Report cards say the fact they claim, and skip the ones they do not have."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers.onboarding import cmd_start
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.report import TOPIC_CUSTOMERS, TOPIC_SYSTEM, ReportOutbox
from database.repositories.user import get_or_create_user
from services.nightly_digest import build_nightly_digest
from services.provisioning import _report_sale
from services.report_events import (
    order_activated,
    parse_admin_customer_start,
    translate_panel_error,
    wallet_changed,
)
from services.report_outbox import enqueue_report
from services.wallet import REASON_CHECKOUT_DEBIT, credit, debit

TEHRAN = ZoneInfo("Asia/Tehran")
# 00:05 on 23 Shahrivar-equivalent: the digest covers the Tehran day before this.
DIGEST_NOW = datetime(2026, 9, 23, 0, 5, tzinfo=TEHRAN)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def test_a_sale_names_the_product_not_the_customer_and_drops_empty_lines() -> None:
    report = order_activated(
        order_id=15,
        telegram_id=537671531,
        username="mojtaba8880",
        is_reseller=True,
        price_toman=225_000,
        product="سرویس دلخواه",
        is_renewal=False,
        service_name="<ali>",
        panel_username="hemmati_1",
        volume_gb=50,
        duration_days=30,
    )
    assert "خرید جدید ثبت شد" in report.body
    assert "تمدید ثبت شد" not in report.body
    assert "محصول: سرویس دلخواه" in report.body
    assert "نام روی سرویس: &lt;ali&gt;" in report.body
    assert "<ali>" not in report.body
    assert "تلفن" not in report.body
    assert "موجودی قبل" not in report.body
    assert "۵۰ گیگ" in report.body
    assert "۲۲۵٬۰۰۰ تومان" in report.body


def test_a_renewal_is_not_worded_as_a_new_purchase() -> None:
    report = order_activated(
        order_id=4,
        telegram_id=7,
        username=None,
        is_reseller=False,
        price_toman=1000,
        product="پلن",
        is_renewal=True,
        balance_before=5_000,
        balance_after=4_000,
    )
    assert "تمدید ثبت شد" in report.body
    assert "خرید جدید" not in report.body
    assert "نام کاربری" not in report.body
    assert "موجودی قبل: ۵٬۰۰۰ تومان" in report.body
    assert "موجودی بعد: ۴٬۰۰۰ تومان" in report.body


def test_a_known_panel_error_does_not_carry_the_host() -> None:
    text = translate_panel_error("Could not resolve host: pasar.example")
    assert text == "میزبان پنل پیدا نشد"
    assert "pasar" not in text
    assert (
        translate_panel_error("email already in use: Testi_0") == "این نام کانفیگ قبلاً ساخته شده"
    )


def test_a_manual_wallet_move_names_the_admin_from_the_idempotency_key() -> None:
    report = wallet_changed(
        entry_id=3,
        telegram_id=537671531,
        name="Mojtaba",
        username="mojtaba8880",
        amount_toman=1_000_000,
        balance_after=1_025_000,
        reason="admin_manual_credit",
        idempotency_key="admin_adjust:560110047:9:stamp",
    )
    assert "موجودی افزایش یافت" in report.body
    assert "شارژ دستی" in report.body
    assert "۵۶۰۱۱۰۰۴۷" in report.body
    assert "موجودی قبل: ۲۵٬۰۰۰ تومان" in report.body


def test_an_admin_customer_payload_is_not_a_referral_code() -> None:
    assert parse_admin_customer_start("admcu_42") == 42
    assert parse_admin_customer_start("ABCD1234") is None
    assert parse_admin_customer_start("admcu_") is None
    assert parse_admin_customer_start("admcu_nope") is None


@pytest.mark.asyncio
async def test_the_sale_card_describes_a_legacy_order_by_its_shape(session: AsyncSession) -> None:
    """``service_name`` is the Latin label, not the product. Using it as the plan lied.

    Nor is it the plan row's name: live that read «volume-10GB-30d-p70000-h0-g6-8».
    An order with no store slug is described by volume and days.
    """
    user = User(telegram_id=555, referral_code="R555", first_name="یاسین", username="yasin")
    session.add(user)
    await session.flush()
    plan = Plan(
        name="volume-50GB-30d-p225000", data_limit_gb=50, duration_days=30, price_toman=225_000
    )
    session.add(plan)
    await session.flush()
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.ACTIVE,
        price_toman=225_000,
        service_name="ali",
        panel_username="ali_1",
    )
    session.add(order)
    await session.flush()

    await _report_sale(session, order)
    body = (await session.execute(select(ReportOutbox.body))).scalar_one()
    assert "محصول: ۵۰ گیگ، ۳۰ روز" in body
    assert "volume-50GB" not in body
    assert "نام روی سرویس: ali" in body
    assert "پلن: ali" not in body


@pytest.mark.asyncio
async def test_a_checkout_debit_does_not_get_its_own_wallet_report(session: AsyncSession) -> None:
    user = User(telegram_id=8, referral_code="R8")
    session.add(user)
    await session.flush()
    await credit(
        session, user_id=user.id, amount_toman=10_000, reason="topup", idempotency_key="seed"
    )
    await debit(
        session,
        user_id=user.id,
        amount_toman=4_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key="checkout_debit:1",
        order_id=None,
    )
    await session.commit()

    reasons = [row.body for row in (await session.execute(select(ReportOutbox))).scalars()]
    assert len(reasons) == 1
    assert "کیف پول شارژ شد" in reasons[0] or "شارژ" in reasons[0]
    assert "از کیف پول کم شد" not in reasons[0]


@pytest.mark.asyncio
async def test_a_first_start_is_reported_once(session: AsyncSession) -> None:
    await get_or_create_user(
        session,
        telegram_id=118609246,
        username="Mehdi_mv8",
        first_name="Mehdi",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
    )
    await get_or_create_user(
        session,
        telegram_id=118609246,
        username="Mehdi_mv8",
        first_name="Mehdi",
        last_name=None,
        language_code="fa",
        admin_telegram_ids=[],
    )
    rows = (await session.execute(select(ReportOutbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].topic_key == TOPIC_CUSTOMERS
    assert rows[0].event_key == "user:118609246:joined"
    assert "@Mehdi_mv8" in rows[0].body
    assert rows[0].link_user_id is not None


@pytest.mark.asyncio
async def test_the_customer_card_link_opens_only_for_an_admin(session: AsyncSession) -> None:
    target = User(telegram_id=42, referral_code="T42", first_name="هدف")
    session.add(target)
    await session.flush()
    target_id = int(target.id)
    await session.commit()

    async def _start(telegram_id: int, admins: list[int]) -> AsyncMock:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=telegram_id,
                username="someone",
                first_name="کس",
                last_name=None,
                language_code="fa",
            ),
            answer=AsyncMock(),
        )
        command = SimpleNamespace(args=f"admcu_{target_id}")
        settings = SimpleNamespace(admin_telegram_ids=admins)
        show = AsyncMock()
        with patch("bot.handlers.admin._show_customer", show):
            await cmd_start(message, command, session, settings, AsyncMock())  # type: ignore[arg-type]
        return show

    admin_show = await _start(560, [560])
    admin_show.assert_awaited_once()
    assert admin_show.await_args.args[2] == target_id

    stranger_show = await _start(111, [560])
    stranger_show.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_nightly_digest_is_the_tehran_day_that_just_ended_and_once(
    session: AsyncSession,
) -> None:
    buyer = User(
        telegram_id=537671531,
        referral_code="M1",
        username="mojtaba8880",
        first_name="Mojtaba",
        is_reseller=True,
    )
    session.add(buyer)
    await session.flush()
    buyer.created_at = datetime(2026, 9, 23, 0, 10, tzinfo=TEHRAN)
    plan = Plan(name="دلخواه", data_limit_gb=50, duration_days=30, price_toman=225_000)
    session.add(plan)
    await session.flush()
    inside = datetime(2026, 9, 22, 18, 0, tzinfo=TEHRAN)
    outside = datetime(2026, 9, 23, 0, 10, tzinfo=TEHRAN)
    session.add_all(
        [
            Order(
                user_id=buyer.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=225_000,
                delivery_data_limit_gb=50,
                activated_at=inside,
            ),
            Order(
                user_id=buyer.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=999_000,
                renew_account_id=1,
                activated_at=inside,
            ),
            Order(
                user_id=buyer.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=50_000,
                activated_at=outside,
            ),
            Order(
                user_id=buyer.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                trial_kind=TRIAL_KIND_PERSONAL,
                activated_at=inside,
            ),
        ]
    )
    newbie = User(telegram_id=99, referral_code="N99", first_name="نو")
    session.add(newbie)
    await session.flush()
    newbie.created_at = inside
    await session.commit()

    async def _enqueue() -> None:
        report = await build_nightly_digest(session, now=DIGEST_NOW)
        await enqueue_report(
            session,
            event_key=report.event_key,
            topic_key=report.topic_key,
            body=report.body,
            link_user_id=report.link_user_id,
        )
        await session.commit()

    await _enqueue()
    await _enqueue()

    rows = (
        (await session.execute(select(ReportOutbox).where(ReportOutbox.topic_key == TOPIC_SYSTEM)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    body = rows[0].body
    assert rows[0].event_key.startswith("digest:")
    assert "خرید تازه: ۱ • ۲۲۵٬۰۰۰ تومان" in body
    assert "تمدید: ۱ • ۹۹۹٬۰۰۰ تومان" in body
    assert "تست: ۱" in body
    assert "حجم فروخته‌شده: ۱۰۰ گیگ" in body
    assert "کاربر جدید: ۱" in body
    assert "۵۰٬۰۰۰" not in body
    assert "@mojtaba8880 • نماینده • ۱٬۲۲۴٬۰۰۰ تومان" in body
