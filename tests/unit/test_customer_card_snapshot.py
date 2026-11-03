"""The admin customer card, byte for byte.

The strings below were recorded from ``bot/handlers/admin.py::_card_text`` on
``main`` (073d646) **before** its facts moved into ``services/customer_card.py``.
The bot's card must not change by one character when the web card shares that
code, so a regression shows here and not on the owner's phone.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import admin as ha
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.customer_lookup import get_customer_card
from services.wallet import credit, debit

JOINED = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def seed(session: AsyncSession) -> dict[str, int]:
    """Three customers; ``last_seen_at`` is the instant of the seed so the age reads «همین الان»."""
    now = datetime.now(UTC)
    referrer = User(
        telegram_id=9001,
        username="boss",
        first_name="رضا",
        language_code="fa",
        referral_code="RBOSS",
        created_at=JOINED,
    )
    session.add(referrer)
    await session.flush()
    alice = User(
        telegram_id=9002,
        username="alice",
        first_name="علی",
        last_name="احمدی",
        language_code="fa",
        referral_code="RALICE",
        referred_by_id=referrer.id,
        created_at=JOINED,
        last_seen_at=now,
    )
    odd = User(
        telegram_id=9003,
        username=None,
        first_name="<b>x</b> & y",
        language_code="fa",
        referral_code="R<&>",
        is_banned=True,
        is_reseller=True,
        is_admin=True,
        created_at=JOINED,
    )
    child = User(
        telegram_id=9004,
        username="kid",
        first_name="K",
        language_code="fa",
        referral_code="RKID",
        referred_by_id=None,
        created_at=JOINED,
    )
    shop = User(
        telegram_id=9005,
        username="shop",
        first_name="فروشگاه",
        language_code="fa",
        referral_code="RSHOP",
        is_reseller=True,
        created_at=JOINED,
    )
    session.add_all([alice, odd, child, shop])
    await session.flush()
    child.referred_by_id = alice.id
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
    for status, price, when in (
        (OrderStatus.ACTIVE, 90_000, datetime(2026, 10, 1, 8, 0, tzinfo=UTC)),
        (OrderStatus.PENDING_PAYMENT, 50_000, datetime(2026, 10, 2, 8, 0, tzinfo=UTC)),
    ):
        session.add(
            Order(
                user_id=alice.id,
                plan_id=plan.id,
                status=status,
                price_toman=price,
                discount_toman=0,
                force_new=True,
                created_at=when,
            )
        )
    await credit(
        session,
        user_id=alice.id,
        amount_toman=200_000,
        reason="admin_manual_credit",
        idempotency_key="snap-1",
    )
    await debit(
        session,
        user_id=alice.id,
        amount_toman=50_000,
        reason="admin_manual_debit",
        idempotency_key="snap-2",
    )
    await credit(
        session,
        user_id=alice.id,
        amount_toman=1_234_567,
        reason="referral_reward",
        idempotency_key="snap-3",
    )
    await session.commit()
    return {"alice": alice.id, "odd": odd.id, "referrer": referrer.id, "shop": shop.id}


GOLDEN: dict[str, str] = {
    "alice": "👤 <b>علی</b>\n@alice • آیدی <code>9002</code>\n✅ فعال • مشتری\n\nعضویت: ۱۴۰۵/۰۷/۰۸ ۱۲:۳۰\nآخرین فعالیت: همین الان\nتست: —\n\n🏦 کیف پول: <b>۱٬۳۸۴٬۵۶۷</b>\nمجموع شارژ: ۱٬۴۳۴٬۵۶۷ • مجموع خرید: ۹۰٬۰۰۰\n🛒 سفارش\u200cها: ۲ (پرداخت\u200cشده: ۱)\nآخرین سفارش: ۱۴۰۵/۰۷/۱۰ ۱۱:۳۰\n\n🎁 زیرمجموعه: ۱ • معرف: @boss\nکد دعوت: <code>RALICE</code>\n\n📱 سرویس\u200cها: ۰\n\nآخرین تراکنش\u200cهای کیف پول:\n  +۱٬۲۳۴٬۵۶۷ • پاداش معرفی\n  −۵۰٬۰۰۰ • کسر دستی ادمین\n  +۲۰۰٬۰۰۰ • شارژ دستی ادمین",
    "odd": "👤 <b>&lt;b&gt;x&lt;/b&gt; &amp; y</b>\nبدون یوزرنیم • آیدی <code>9003</code>\n🚫 مسدود • ⚙️ ادمین\n\nعضویت: ۱۴۰۵/۰۷/۰۸ ۱۲:۳۰\nآخرین فعالیت: —\nتست: —\n\n🏦 کیف پول: <b>۰</b>\nمجموع شارژ: ۰ • مجموع خرید: ۰\n🛒 سفارش\u200cها: ۰ (پرداخت\u200cشده: ۰)\nآخرین سفارش: —\n\n🎁 زیرمجموعه: ۰ • معرف: —\nکد دعوت: <code>R&lt;&amp;&gt;</code>\n\n📱 سرویس\u200cها: ۰\n\nکیف پول تراکنشی ندارد.",
    "referrer": "👤 <b>رضا</b>\n@boss • آیدی <code>9001</code>\n✅ فعال • مشتری\n\nعضویت: ۱۴۰۵/۰۷/۰۸ ۱۲:۳۰\nآخرین فعالیت: —\nتست: —\n\n🏦 کیف پول: <b>۰</b>\nمجموع شارژ: ۰ • مجموع خرید: ۰\n🛒 سفارش\u200cها: ۰ (پرداخت\u200cشده: ۰)\nآخرین سفارش: —\n\n🎁 زیرمجموعه: ۱ • معرف: —\nکد دعوت: <code>RBOSS</code>\n\n📱 سرویس\u200cها: ۰\n\nکیف پول تراکنشی ندارد.",
    "shop": "👤 <b>فروشگاه</b>\n@shop • آیدی <code>9005</code>\n✅ فعال • 🏪 نماینده\n\nعضویت: ۱۴۰۵/۰۷/۰۸ ۱۲:۳۰\nآخرین فعالیت: —\nتست: —\n\n🏦 کیف پول: <b>۰</b>\nمجموع شارژ: ۰ • مجموع خرید: ۰\n🛒 سفارش\u200cها: ۰ (پرداخت\u200cشده: ۰)\nآخرین سفارش: —\n\n🎁 زیرمجموعه: ۰ • معرف: —\nکد دعوت: <code>RSHOP</code>\n\n📱 سرویس\u200cها: ۰\n\nکیف پول تراکنشی ندارد.",
}


@pytest.mark.asyncio
async def test_the_bots_card_text_is_unchanged(session: AsyncSession) -> None:
    ids = await seed(session)
    for key in ("alice", "odd", "referrer", "shop"):
        text = ha._card_text(await get_customer_card(session, ids[key]))
        assert text == GOLDEN[key]
