"""The finance page shows the figures the bot's «گزارش مالی» shows."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.texts import texts
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.admin import dashboard_counts
from services.reporting import get_finance_report
from services.ux_format import fa_toman, to_persian_digits
from tests.unit.web_panel_helpers import ENV_ADMIN, INIT_HEADER, init_data, panel_client

Factory = tuple[async_sessionmaker[AsyncSession], str]


async def _seed(factory: async_sessionmaker[AsyncSession], *, waiting: bool) -> None:
    async with factory() as session:
        buyer = User(
            telegram_id=501,
            username="b",
            first_name="B",
            language_code="fa",
            referral_code="R501",
            wallet_balance_toman=789_000,
        )
        banned = User(
            telegram_id=502,
            username="x",
            first_name="X",
            language_code="fa",
            referral_code="R502",
            is_banned=True,
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
        session.add_all([buyer, banned, plan])
        await session.flush()
        now = datetime.now(UTC)
        session.add(
            Order(
                user_id=buyer.id,
                plan_id=plan.id,
                discount_toman=0,
                force_new=True,
                status=OrderStatus.ACTIVE,
                price_toman=123_000,
                activated_at=now,
            )
        )
        # Distinct credited and spent totals: a swapped cell must show.
        session.add_all(
            [
                WalletLedgerEntry(
                    user_id=buyer.id,
                    entry_type=WalletEntryType.credit,
                    amount_toman=910_000,
                    reason="admin_credit",
                    idempotency_key="seed-credit",
                    balance_after_toman=910_000,
                ),
                WalletLedgerEntry(
                    user_id=buyer.id,
                    entry_type=WalletEntryType.debit,
                    amount_toman=121_000,
                    reason="checkout_debit",
                    idempotency_key="seed-debit",
                    balance_after_toman=789_000,
                ),
            ]
        )
        if waiting:
            session.add(
                Order(
                    user_id=banned.id,
                    plan_id=plan.id,
                    discount_toman=0,
                    force_new=True,
                    status=OrderStatus.PAYMENT_REVIEW,
                    price_toman=40_000,
                )
            )
        await session.commit()


async def _page(client) -> str:
    resp = await client.get("/panel/finance", headers={INIT_HEADER: init_data(ENV_ADMIN)})
    assert resp.status == 200
    return await resp.text()


@pytest.mark.asyncio
async def test_the_page_shows_every_figure_the_bot_screen_shows(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await _seed(factory, waiting=True)
    async with factory() as session:
        report = await get_finance_report(session)
        counts = await dashboard_counts(session)
    assert report.today.revenue_toman == 123_000, "the seed must produce a visible sale"

    async with panel_client(factory) as client:
        html = await _page(client)

    for window in (report.today, report.week, report.month, report.jalali_month, report.all_time):
        assert window.label_fa in html
        assert fa_toman(window.revenue_toman) in html
        assert f"{to_persian_digits(window.orders)} سفارش" in html
        assert fa_toman(window.average_toman) in html
    for figure in (
        fa_toman(report.wallet_liability_toman),
        fa_toman(report.wallet_credited_toman),
        fa_toman(report.wallet_spent_toman),
        to_persian_digits(report.active_services),
        to_persian_digits(report.paying_customers),
        to_persian_digits(counts.users),
        to_persian_digits(counts.banned_users),
    ):
        assert figure in html
    assert fa_toman(789_000) in html, "the wallet liability is the seeded balance"
    assert (report.wallet_credited_toman, report.wallet_spent_toman) == (910_000, 121_000)
    credited = html.split("مجموع شارژشده</span>")[1].split("</b>")[0]
    spent = html.split("مجموع خرج‌شده</span>")[1].split("</b>")[0]
    assert fa_toman(910_000) in credited and fa_toman(121_000) in spent
    assert "123" not in html and "789" not in html, "figures are Persian digits, not Latin"


@pytest.mark.asyncio
async def test_the_labels_are_the_bots_labels(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await _seed(factory, waiting=True)
    async with panel_client(factory) as client:
        html = await _page(client)
    for label in (
        "فروش",
        "کیف پول مشتری‌ها",
        "وضعیت",
        "بدهی فعلی (قابل خرج)",
        "مجموع شارژشده",
        "مجموع خرج‌شده",
        "سرویس ثبت‌شده",
        "مشتری خریدار",
        "کاربران",
        "مسدود",
    ):
        assert label in texts.admin_stats, f"the bot no longer says «{label}»"
        assert label in html
    assert texts.admin_stats_note.split("،")[0] in html
    assert texts.web_stats_attention.format(review="۱", failed="۰") in html


@pytest.mark.asyncio
async def test_the_attention_line_is_calm_when_nothing_waits(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await _seed(factory, waiting=False)
    async with panel_client(factory) as client:
        html = await _page(client)
    assert texts.admin_stats_attention_none in html
    assert "نیازمند رسیدگی" not in html


@pytest.mark.asyncio
async def test_the_shell_carries_no_report_data(two_sessions: Factory) -> None:
    """The shell is public and static: it must not read the database or print a figure."""
    factory, _ = two_sessions
    await _seed(factory, waiting=True)
    untouched = MagicMock(side_effect=AssertionError("the shell opened a database session"))
    async with panel_client(untouched) as client:
        resp = await client.get("/panel/")
        assert resp.status == 200
        html = await resp.text()
    for datum in ("۱۲۳٬۰۰۰", "۷۸۹٬۰۰۰", "123", "789", "501", "گزارش مالی"):
        assert datum not in html
    # The shell asks who is looking; it names no screen of its own, so a public
    # page carries no admin link.
    assert 'hx-get="/panel/me/home"' in html
    assert "/panel/finance" not in html
    assert "telegram-web-app.js" in html
    assert "/panel/static/htmx.min.js" in html, "htmx is served by us, not by a CDN"
