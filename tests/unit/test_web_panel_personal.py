"""The customer page and the reseller dashboard: their own data, and only that.

Every seeded person carries values nobody else has (names, panel usernames,
link tokens, amounts), so a page that leaks another person's data shows it.
The panel is a fake ``get_user``; nothing here reaches a real one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api_client.exceptions import PasarGuardAPIError
from database.models import OrderStatus
from services.reporting import FinanceWindow, get_finance_report, user_sales
from services.trial import reseller_month_sales
from services.ux_format import fa_toman, to_persian_digits
from services.wallet import wallet_reason_label
from tests.unit.web_panel_helpers import INIT_HEADER, init_data
from tests.unit.web_personal_helpers import (
    Factory,
    FakePanel,
    make_ledger,
    make_person,
    make_sale,
    make_service,
    make_trials,
    mark_panel_down,
    panel_user,
    personal_client,
    use_panel,
)

CUST_A, CUST_B = 5001, 5002
SHOP_A, SHOP_B = 5101, 5102
Pair = tuple[Factory, str]


async def _page(client, tg: int, path: str) -> str:  # type: ignore[no-untyped-def]
    resp = await client.get(path, headers={INIT_HEADER: init_data(tg)})
    assert resp.status == 200, (path, resp.status)
    return await resp.text()


# --- isolation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_customer_never_sees_another_customers_data(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    a = await make_person(factory, CUST_A, name="اولی", balance=111_111)
    b = await make_person(factory, CUST_B, name="دومی", balance=777_777)
    await make_service(factory, a, username="alpha_u1", label="سرویس-الف")
    await make_service(factory, b, username="bravo_u2", label="سرویس-ب")
    await make_ledger(factory, a, amount=11_000, credit=True, reason="topup", after=111_111)
    await make_ledger(
        factory,
        b,
        amount=666_000,
        credit=True,
        reason="referral_reward",
        after=777_777,
        note="یادداشت-محرمانه-ب",
    )
    use_panel(
        monkeypatch,
        FakePanel(
            {
                "alpha_u1": panel_user("alpha_u1", used_gb=1.0),
                "bravo_u2": panel_user("bravo_u2", used_gb=9.0),
            }
        ),
    )
    async with personal_client(factory) as client:
        html_a = await _page(client, CUST_A, "/panel/me")
        html_b = await _page(client, CUST_B, "/panel/me")

    assert "سرویس-الف" in html_a and fa_toman(111_111) in html_a and fa_toman(11_000) in html_a
    for hers in ("سرویس-ب", fa_toman(777_777), fa_toman(666_000), "یادداشت-محرمانه-ب", "bravo_u2"):
        assert hers not in html_a, hers
    assert "سرویس-ب" in html_b
    for his in ("سرویس-الف", fa_toman(111_111), fa_toman(11_000), "alpha_u1"):
        assert his not in html_b, his


@pytest.mark.asyncio
async def test_a_reseller_never_sees_another_resellers_data(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    a = await make_person(factory, SHOP_A, name="فروشگاه-الف", reseller=True, balance=135_000)
    b = await make_person(factory, SHOP_B, name="فروشگاه-ب", reseller=True, balance=864_000)
    for uid, tag, sale in ((a, "A", 123_000), (b, "B", 987_000)):
        await make_sale(factory, uid, price=sale)
        await make_service(
            factory, uid, username=f"shop{tag.lower()}_u1", label=f"رضایی-{tag}-مشتری"
        )
        await make_ledger(
            factory,
            uid,
            amount=sale // 3,
            credit=False,
            reason="checkout_debit",
            after=1,
        )
    await make_trials(factory, a, count=1, quota=3)
    await make_trials(factory, b, count=4, quota=9)
    use_panel(
        monkeypatch,
        FakePanel({"shopa_u1": panel_user("shopa_u1"), "shopb_u1": panel_user("shopb_u1")}),
    )
    async with personal_client(factory) as client:
        html_a = await _page(client, SHOP_A, "/panel/me/reseller")
        html_b = await _page(client, SHOP_B, "/panel/me/reseller")

    assert "رضایی-A-مشتری" in html_a and fa_toman(123_000) in html_a and fa_toman(135_000) in html_a
    for his in (
        "رضایی-B-مشتری",
        "shopb_u1",
        fa_toman(987_000),
        fa_toman(864_000),
        fa_toman(329_000),  # B's ledger entry: 987_000 // 3
        "فروشگاه-ب",
    ):
        assert his not in html_a, his
    assert "رضایی-B-مشتری" in html_b and fa_toman(987_000) in html_b
    for hers in (
        "رضایی-A-مشتری",
        "shopa_u1",
        fa_toman(123_000),
        fa_toman(135_000),
        fa_toman(41_000),
    ):
        assert hers not in html_b, hers


@pytest.mark.asyncio
async def test_a_good_panel_read_is_cached_per_person_and_service(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    a = await make_person(factory, CUST_A)
    b = await make_person(factory, CUST_B)
    await make_service(factory, a, username="alpha_u1", label="سرویس-الف")
    await make_service(factory, b, username="bravo_u2", label="سرویس-ب")
    panel = use_panel(
        monkeypatch,
        FakePanel(
            {
                "alpha_u1": panel_user("alpha_u1", used_gb=1.0),
                "bravo_u2": panel_user("bravo_u2", used_gb=9.0),
            }
        ),
    )
    async with personal_client(factory) as client:
        await _page(client, CUST_A, "/panel/me")
        await _page(client, CUST_B, "/panel/me")
        await _page(client, CUST_A, "/panel/me")
    assert panel.reads == ["alpha_u1", "bravo_u2"], "each person reads once; the repeat is cached"


# --- what the customer page shows ----------------------------------------------


@pytest.mark.asyncio
async def test_the_customer_page_shows_services_usage_expiry_and_wallet(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    uid = await make_person(factory, CUST_A, balance=250_000)
    await make_service(factory, uid, username="alpha_u1", label="سرویس-الف", expires_in_days=30)
    await make_ledger(factory, uid, amount=250_000, credit=True, reason="topup", after=250_000)
    await make_ledger(
        factory, uid, amount=40_000, credit=False, reason="checkout_debit", after=210_000
    )
    use_panel(
        monkeypatch,
        FakePanel(
            {"alpha_u1": panel_user("alpha_u1", used_gb=2.5, limit_gb=10, expires_in_days=12)}
        ),
    )
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")

    assert "سرویس-الف" in html
    assert "فعال" in html  # the panel's status
    assert "۲٫۵۰ گیگ" in html and "۱۰ گیگ" in html and "۲۵٪" in html
    assert "۱۲ روز" in html  # time left, from the panel's expiry, not the order's 30
    assert fa_toman(250_000) in html
    assert wallet_reason_label("topup") in html and wallet_reason_label("checkout_debit") in html
    assert "−" + fa_toman(40_000) in html and "+" + fa_toman(250_000) in html


@pytest.mark.asyncio
async def test_a_customer_with_no_service_and_no_entries_gets_plain_lines(
    two_sessions: Pair,
) -> None:
    factory, _ = two_sessions
    await make_person(factory, CUST_A)
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")
    assert "هنوز سرویسی نداری" in html and "هنوز تراکنشی نداری" in html
    assert fa_toman(0) in html


# --- panel reads are best-effort -------------------------------------------------


async def _seed_one_customer(factory: Factory) -> None:
    uid = await make_person(factory, CUST_A, balance=5_000)
    await make_service(factory, uid, username="alpha_u1", label="سرویس-الف", expires_in_days=9)


def _no_usage(html: str) -> None:
    for usage_line in ("حجم مصرف‌شده", "سقف حجم", "مصرف حجم", "وضعیت:"):
        assert usage_line not in html, usage_line
    # The order's own expiry date is still true and still shown.
    assert "انقضا:" in html and "سرویس-الف" in html


LEAKS = ("Traceback", "TimeoutError", "PasarGuard", "secret-detail", "Error", "Exception")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        PasarGuardAPIError("secret-detail 500 from panel"),
        TimeoutError("secret-detail timed out"),
        RuntimeError("secret-detail"),
    ],
)
async def test_a_failed_panel_read_drops_the_usage_and_leaks_nothing(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    factory, _ = two_sessions
    await _seed_one_customer(factory)
    use_panel(monkeypatch, FakePanel({"alpha_u1": failure}))
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")
    _no_usage(html)
    for leak in LEAKS:
        assert leak not in html, leak


@pytest.mark.asyncio
async def test_a_panel_client_that_cannot_be_built_drops_the_usage_too(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import personal_page

    async def broken(*_a: object, **_kw: object) -> None:
        raise RuntimeError("secret-detail: bad key")

    monkeypatch.setattr(personal_page, "get_pasarguard_client", broken)
    factory, _ = two_sessions
    await _seed_one_customer(factory)
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")
    _no_usage(html)
    assert "secret-detail" not in html


@pytest.mark.asyncio
async def test_a_panel_confirmed_down_is_not_asked_and_drops_the_usage(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    await _seed_one_customer(factory)
    await mark_panel_down(factory)
    panel = use_panel(monkeypatch, FakePanel({"alpha_u1": panel_user("alpha_u1")}))
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")
    assert panel.reads == [], "a panel known to be down is not asked"
    _no_usage(html)


@pytest.mark.asyncio
async def test_a_slow_panel_read_gives_up_and_the_page_still_renders(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from services import personal_page

    class Slow(FakePanel):
        async def get_user(self, username: str):  # type: ignore[no-untyped-def]
            await asyncio.sleep(5)
            return panel_user(username)

    monkeypatch.setattr(personal_page, "PANEL_READ_TIMEOUT_SECONDS", 0.05)
    factory, _ = two_sessions
    await _seed_one_customer(factory)
    use_panel(monkeypatch, Slow({}))
    async with personal_client(factory) as client:
        html = await _page(client, CUST_A, "/panel/me")
    _no_usage(html)


@pytest.mark.asyncio
async def test_a_failed_read_is_not_cached(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next page load asks again instead of showing a minute of nothing."""
    factory, _ = two_sessions
    await _seed_one_customer(factory)
    panel = use_panel(monkeypatch, FakePanel({"alpha_u1": TimeoutError("x")}))
    async with personal_client(factory) as client:
        _no_usage(await _page(client, CUST_A, "/panel/me"))
        panel.users["alpha_u1"] = panel_user("alpha_u1", used_gb=3.0)
        assert "مصرف حجم" in await _page(client, CUST_A, "/panel/me")
    assert panel.reads == ["alpha_u1", "alpha_u1"]


# --- no credential, no offer ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subscription_link_or_token_appears_on_either_page(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    cust = await make_person(factory, CUST_A)
    shop = await make_person(factory, SHOP_A, reseller=True)
    link_c, link_s = (
        "https://sub.example.net/s/CUSTOMER-SECRET-TOKEN-9f3",
        "https://sub.example.net/s/SHOP-SECRET-TOKEN-7c1",
    )
    await make_service(factory, cust, username="alpha_u1", label="سرویس-الف", link=link_c)
    await make_service(factory, shop, username="shopa_u1", label="رضایی-مشتری", link=link_s)
    use_panel(
        monkeypatch,
        FakePanel({"alpha_u1": panel_user("alpha_u1"), "shopa_u1": panel_user("shopa_u1")}),
    )
    async with personal_client(factory) as client:
        pages = [
            await _page(client, CUST_A, "/panel/me"),
            await _page(client, SHOP_A, "/panel/me/reseller"),
            await _page(client, SHOP_A, "/panel/me"),
        ]
    for html in pages:
        for secret in (
            "SECRET-TOKEN",
            "sub.example.net",
            "panel.example.com/sub",
            "https://",
            "http://",
        ):
            assert secret not in html, secret
        assert (
            "لینک سابسکریپشن" not in html
            and "QR" not in html
            and "qr" not in html.lower().replace("query", "")
        )


@pytest.mark.asyncio
async def test_no_page_states_a_discount_an_offer_or_a_nudge(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    await make_person(factory, CUST_A)
    await make_person(factory, SHOP_A, reseller=True)
    async with personal_client(factory) as client:
        pages = [
            await _page(client, CUST_A, "/panel/me"),
            await _page(client, SHOP_A, "/panel/me/reseller"),
        ]
    for html in pages:
        for word in ("تخفیف", "پیشنهاد", "کد ", "جایزه", "هدیه", "فقط تا", "محدود", "ارزون"):
            assert word not in html, word


# --- the reseller dashboard ----------------------------------------------------------


async def _seed_sales(factory: Factory, shop: int, other: int) -> bool:
    """Orders that count, orders that must not, and another shop's.

    Returns whether an order from earlier this Jalali month was seeded: it
    counts for the month and not for today, which is what tells the month
    window from the day window. On the first day of the month there is no
    such moment, and the month and today then coincide.
    """
    from services.reporting import window_start
    from services.trial import _jalali_month_start

    now = datetime.now(UTC)
    earlier = _jalali_month_start(now) + timedelta(hours=1)
    early = earlier < window_start(now, 1)
    if early:
        await make_sale(factory, shop, price=12_000, activated_at=earlier)
    await make_sale(factory, shop, price=50_000)  # counts: today, month, all
    await make_sale(factory, shop, price=30_000, status=OrderStatus.PROVISIONING)  # counts
    await make_sale(factory, shop, price=0)  # a giveaway: not a sale
    await make_sale(
        factory, shop, price=44_000, status=OrderStatus.PENDING_PAYMENT, activated_at=None
    )  # not paid yet
    await make_sale(factory, shop, price=45_000, status=OrderStatus.REJECTED)  # rejected
    await make_sale(
        factory, shop, price=70_000, activated_at=now - timedelta(days=60)
    )  # all-time only
    await make_sale(factory, other, price=999_000)  # someone else's
    return early


@pytest.mark.asyncio
async def test_the_sales_figures_are_the_existing_definition_of_a_sale(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    shop = await make_person(factory, SHOP_A, reseller=True)
    other = await make_person(factory, SHOP_B, reseller=True)
    early = await _seed_sales(factory, shop, other)
    use_panel(monkeypatch, FakePanel({}))
    extra_count, extra_amount = (1, 12_000) if early else (0, 0)

    async with factory() as session:
        today, month, total = await user_sales(session, shop)
        month_count = await reseller_month_sales(session, shop)
    assert (today.orders, today.revenue_toman) == (2, 80_000)
    assert (month.orders, month.revenue_toman) == (2 + extra_count, 80_000 + extra_amount)
    assert (total.orders, total.revenue_toman) == (3 + extra_count, 150_000 + extra_amount)
    assert month.orders == month_count, "the same count the owner's reseller list prints"

    async with personal_client(factory) as client:
        html = await _page(client, SHOP_A, "/panel/me/reseller")
    for window in (today, month, total):
        amount, count = window.revenue_toman, window.orders
        assert window.label_fa in html
        assert fa_toman(amount) in html and f"{to_persian_digits(count)} سرویس" in html
    for must_not in (fa_toman(999_000), fa_toman(44_000), fa_toman(45_000), fa_toman(1_079_000)):
        assert must_not not in html, must_not


@pytest.mark.asyncio
async def test_a_user_window_is_the_finance_report_narrowed_to_one_user(two_sessions: Pair) -> None:
    """With only one shop in the database, its windows are the owner's report."""
    factory, _ = two_sessions
    shop = await make_person(factory, SHOP_A, reseller=True)
    now = datetime.now(UTC)
    await make_sale(factory, shop, price=50_000)
    await make_sale(factory, shop, price=0)
    await make_sale(factory, shop, price=70_000, activated_at=now - timedelta(days=60))
    async with factory() as session:
        report = await get_finance_report(session)
        today, month, total = await user_sales(session, shop)
    same = lambda w: (w.orders, w.revenue_toman)  # noqa: E731
    assert same(today) == same(report.today)
    assert same(month) == same(report.jalali_month)
    assert same(total) == same(report.all_time)
    assert isinstance(today, FinanceWindow)


@pytest.mark.asyncio
async def test_the_dashboard_shows_services_by_their_customers_name_paged(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    shop = await make_person(factory, SHOP_A, reseller=True)
    panel_users = {}
    for i in range(12):
        name = f"u{i:02d}"
        await make_service(factory, shop, username=f"shop_{name}", label=f"رضایی-{name}")
        panel_users[f"shop_{name}"] = panel_user(f"shop_{name}")
    use_panel(monkeypatch, FakePanel(panel_users))
    async with personal_client(factory) as client:
        first = await _page(client, SHOP_A, "/panel/me/reseller")
        second = await _page(client, SHOP_A, "/panel/me/reseller?offset=10")
        junk = await _page(client, SHOP_A, "/panel/me/reseller?offset=-5")
        junk2 = await _page(client, SHOP_A, "/panel/me/reseller?offset=abc")
    assert "رضایی-u11" in first and "رضایی-u02" in first and "رضایی-u01" not in first
    assert "۱۲ سرویس • نمایش ۱ تا ۱۰" in first and "offset=10" in first
    assert "رضایی-u01" in second and "رضایی-u00" in second and "رضایی-u11" not in second
    assert "۱۲ سرویس • نمایش ۱۱ تا ۱۲" in second and "offset=0" in second
    assert "رضایی-u11" in junk and "رضایی-u11" in junk2, "a bad offset means the first page"


@pytest.mark.asyncio
async def test_the_dashboard_shows_the_trial_quota_and_the_wallet(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    shop = await make_person(factory, SHOP_A, reseller=True, balance=64_000)
    await make_trials(factory, shop, count=2, quota=5)
    await make_ledger(factory, shop, amount=64_000, credit=True, reason="topup", after=64_000)
    async with personal_client(factory) as client:
        html = await _page(client, SHOP_A, "/panel/me/reseller")
    assert "۲ از ۵" in html and "۳ تا مونده" in html
    assert fa_toman(64_000) in html and wallet_reason_label("topup") in html


@pytest.mark.asyncio
async def test_a_spent_quota_says_what_the_bot_says(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    paid = await make_person(factory, SHOP_A, reseller=True)
    await make_trials(factory, paid, count=2, quota=2, price=15_000)
    free_end = await make_person(factory, SHOP_B, reseller=True)
    await make_trials(factory, free_end, count=1, quota=1, price=0)
    async with personal_client(factory) as client:
        html_paid = await _page(client, SHOP_A, "/panel/me/reseller")
        html_none = await _page(client, SHOP_B, "/panel/me/reseller")
    assert "سهمیهٔ رایگانت تموم شده" in html_paid and fa_toman(15_000) in html_paid
    assert "قیمتی برای تست بیشتر تنظیم نشده" in html_none


@pytest.mark.asyncio
async def test_the_pages_write_nothing(two_sessions: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening either page leaves every table as it was."""
    from sqlalchemy import func, select

    from database.models import Order, PasarGuardAccount, User, WalletLedgerEntry

    factory, _ = two_sessions
    shop = await make_person(factory, SHOP_A, reseller=True, balance=10)
    await make_service(factory, shop, username="shopa_u1", label="رضایی")
    use_panel(monkeypatch, FakePanel({"shopa_u1": panel_user("shopa_u1")}))

    async def snapshot() -> tuple[object, ...]:
        async with factory() as session:
            counts = [
                await session.scalar(select(func.count()).select_from(m))
                for m in (User, Order, PasarGuardAccount, WalletLedgerEntry)
            ]
            account = (await session.execute(select(PasarGuardAccount))).scalar_one()
            user = (await session.execute(select(User))).scalar_one()
            return (
                *counts,
                account.updated_at,
                account.subscription_url,
                user.updated_at,
                user.wallet_balance_toman,
            )

    before = await snapshot()
    async with personal_client(factory) as client:
        await _page(client, SHOP_A, "/panel/me/reseller")
        await _page(client, SHOP_A, "/panel/me")
        await _page(client, SHOP_A, "/panel/me/home")
    assert await snapshot() == before
