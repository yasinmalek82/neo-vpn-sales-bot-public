"""Section 15, part two: the broadcast test send, the finance month, gifted days, a deleted panel
account, and re-attaching the report group.

Rows ۱۵.۵ to ۱۵.۹.
"""

from __future__ import annotations

import jdatetime
import pytest
from sqlalchemy import select

from database.models import Order, OrderStatus
from database.models.broadcast import Broadcast
from database.models.report import OutboxState, ReportOutbox
from tests.e2e._s07_helpers import ledger, take_trial
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active, expire_in, my_service, open_service
from tests.e2e.harness import App, Store, digits, publish_store, smoke

SIGNATURE = "نئو نتورک · @neo_test_bot"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


# ------------------------------------------------------------------ the test send


@smoke("۱۵.۵")
async def test_the_test_send_goes_only_to_the_owner_and_the_confirm_page_stays(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "پیام همگانی")
    await tap_starting(admin, "پیام جدید")
    await admin.send("سلام، سرور جدید اضافه شد")
    await tap_starting(admin, "همهٔ مشتری")
    confirm = admin.last_message
    assert confirm.text.startswith("⚠️ ارسال به 1 نفر")
    buttons_before = confirm.buttons
    marks = {"admin": admin.mark(), "ali": ali.mark()}
    await tap_starting(admin, "🧪 اول برای خودم بفرست")

    sent = [m for m in admin.messages_since(marks["admin"]) if m.text.startswith("سلام، سرور جدید")]
    assert len(sent) == 1, sent
    assert sent[0].text.endswith(SIGNATURE) and "اضافه شد" in sent[0].text.split("─────")[0], sent
    assert "ارسالی شروع نشد" in (admin.popup or "")
    assert confirm.buttons == buttons_before, "the confirm page is where it was"
    assert "📤 بله، ارسال کن" in confirm.buttons
    assert not await app.all(select(Broadcast)), "no campaign was created"
    await app.run_job("broadcast")
    assert ali.messages_since(marks["ali"]) == [], "the customer got nothing"


# --------------------------------------------------------------------- the finance month


def _finance_lines(text: str) -> dict[str, str]:
    return {ln.split(":")[0]: ln for ln in text.splitlines() if ":" in ln}


@smoke("۱۵.۶")
async def test_this_month_row_sits_between_30_days_and_the_start_and_counts_paid_sales_of_the_jalali_month(
    app: App, store: Store
) -> None:
    sale, old, giveaway = (app.customer(n) for n in ("sale", "old", "gift"))
    await buy_active(app, store, sale, "special_50", "sale1")
    await buy_active(app, store, old, "special_50", "old1")
    old_order = await app.latest_order(old)
    # Last Jalali month for certain: the number of days into this one, and three more.
    into_month = jdatetime.datetime.now().day - 1
    await app.age(Order, old_order.id, days=into_month + 3, columns=("activated_at",))
    await giveaway.start()
    await take_trial(giveaway)  # a free trial: not a sale

    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "فروش و سفارش")
    await tap_starting(admin, "گزارش مالی")
    lines = admin.last_text.splitlines()
    labels = [
        ln.split(":")[0]
        for ln in lines
        if ln.startswith(("امروز", "7 روز", "30 روز", "این ماه", "از ابتدا"))
    ]
    assert labels == ["امروز", "7 روز اخیر", "30 روز اخیر", "این ماه", "از ابتدا"], labels
    rows = _finance_lines(admin.last_text)
    this_month, since_start = digits(rows["این ماه"]), digits(rows["از ابتدا"])
    assert (
        "150000" in this_month.replace(",", "").replace("٬", "") and "1 سفارش" in this_month
    ), this_month
    assert "300000" in since_start.replace(",", "").replace("٬", "") and "2 سفارش" in since_start
    if into_month + 3 <= 29:
        assert "2 سفارش" in digits(
            rows["30 روز اخیر"]
        ), "the trailing 30 days is a different window"


# ------------------------------------------------------------ gifted days are not value


def _worth(text: str) -> int:
    line = next(ln for ln in text.splitlines() if "ارزش روزهای باقی" in ln)
    return int(digits(line.split(":")[1]).replace("٬", "").replace(",", "").split()[0])


@smoke("۱۵.۷")
async def test_extending_thirty_days_does_not_raise_the_value_of_the_unused_days(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)  # 150 000 for 30 days
    order = await app.latest_order(ali)
    # 10 days minus an hour: the paid period then ends 20 days and an hour ahead,
    # so the whole-day floor cannot tip to 19 when a second ticks mid-scenario.
    await app.age(Order, order.id, days=10, minutes=-60, columns=("activated_at",))
    expire_in(account, days=20)
    expire_before = account.expire
    admin = app.admin()
    await open_service(admin, "ali")
    before = _worth(admin.last_text)
    assert before == 100_000, admin.last_text  # 150 000 ÷ 30 × 20 days
    await tap_starting(admin, "📅 تمدید")
    await admin.send("30")
    assert (account.expire - expire_before).days >= 29, "the panel did move"
    await tap_starting(admin, "⬅️ سرویس‌ها")
    await admin.tap(next(b for b in admin.buttons if b.startswith("🟢")))
    assert _worth(admin.last_text) == before, "gifted days are not paid days"


# ------------------------------------------------------------ the account is gone


@smoke("۱۵.۸")
async def test_renewing_an_account_deleted_in_the_panel_refunds_once_and_tells_the_admin(
    app: App, store: Store
) -> None:
    group = await app.connect_report_group()
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    await app.give_wallet(ali, 150_000)
    del app.panel.users[account.username]  # the owner deleted it in the panel itself
    await my_service(ali)
    await tap_starting(ali, "تمدید")
    await tap_starting(ali, "بدون کد")
    assert "کسر از کیف پول: 150000" in digits(ali.last_text)
    mark = ali.mark()
    await tap_starting(ali, "ثبت سفارش")
    order = await app.latest_order(ali)
    assert order.status == OrderStatus.PROVISION_FAILED
    # The summary is edited too (its buttons go), so it is among these.
    assert any("به کیف پولت برگشت" in t for t in ali.texts_since(mark))
    assert await app.wallet_balance(ali) == 150_000, "the money came back"
    refunds = [
        e
        for e in await ledger(app, ali)
        if e.order_id == order.id and e.entry_type.value == "credit"
    ]
    assert len(refunds) == 1, "once"
    assert app.panel.users == {}, "no new service was made"
    assert len(app.panel.requests_to("POST /api/user")) == 1, "only the first purchase created one"

    await app.run_job("report_flush")
    errors = [t for t in await group.texts("errors") if "تست مسیر" not in t]
    assert len(errors) == 1 and "سرویس در پنل پیدا نشد" in errors[0], errors
    assert f"کد سفارش: {order.id}" in digits(errors[0]), errors[0]
    await app.run_job("report_flush")
    assert len([t for t in await group.texts("errors") if "تست مسیر" not in t]) == 1


# ---------------------------------------------------------------- the report group


async def _reattach(app: App, admin, group) -> str:
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "گروه گزارش")
    await tap_starting(admin, "تغییر گروه")
    await admin.send(str(group.chat.id))
    return admin.last_text


async def _lost_sales_report(app: App, store: Store, group) -> None:
    """A sale whose «sales» topic vanished: the report burns its five tries and is retired."""
    ali = app.customer("ali")
    sales_thread = (await group._threads())["sales"]  # noqa: SLF001
    del group.chat.topics[sales_thread]
    await buy_active(app, store, ali)
    for _ in range(6):
        await app.run_job("report_flush")
    failed = await app.all(select(ReportOutbox).where(ReportOutbox.state == OutboxState.failed))
    assert [r.topic_key for r in failed] == ["sales"]


@smoke("۱۵.۹")
async def test_reattaching_the_same_group_requeues_last_weeks_failed_reports_into_their_topics(
    app: App, store: Store
) -> None:
    app.tg.accept("no_thread")
    group = await app.connect_report_group()
    await _lost_sales_report(app, store, group)
    admin = app.admin()
    text = await _reattach(app, admin, group)
    assert "🔁 1 گزارش که قبلاً نرفته بود دوباره در صف تحویل گذاشته شد." in text, text
    await app.run_job("report_flush")
    sales = [t for t in await group.texts("sales") if "تست مسیر" not in t]
    assert len(sales) == 1 and "خرید جدید ثبت شد" in sales[0], sales
    assert not await app.all(select(ReportOutbox).where(ReportOutbox.state == OutboxState.failed))


@smoke("۱۵.۹")
async def test_reattaching_with_nothing_failed_or_only_old_failures_says_nothing_about_requeueing(
    app: App, store: Store
) -> None:
    app.tg.accept("no_thread")
    group = await app.connect_report_group()
    admin = app.admin()
    assert "دوباره در صف" not in await _reattach(app, admin, group), "nothing failed"
    await _lost_sales_report(app, store, group)
    failed = await app.all(select(ReportOutbox).where(ReportOutbox.state == OutboxState.failed))
    await app.age(ReportOutbox, failed[0].id, days=8, columns=("created_at",))
    assert "دوباره در صف" not in await _reattach(app, admin, group), "eight days old is too old"
    await app.run_job("report_flush")
    assert [t for t in await group.texts("sales") if "تست مسیر" not in t] == []
