"""Real checkout -> authenticated SMS -> recovery -> one customer service card."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.texts import texts
from database.models import BankInvoice, Order, OrderStatus, Payment, WalletLedgerEntry
from services.bank_sms import receive_bank_sms
from tests.e2e.harness import App, order_by_card, publish_store
from web.bank_sms import mount_bank_sms


def enable_bank_sms(app: App) -> None:
    app.settings.bank_sms_enabled = True
    app.settings.scheduler_enabled = True
    app.settings.bank_sms_token = "x" * 40
    app.settings.bank_sms_sender = "BluBank"
    app.settings.bank_sms_account_key = "blu-test"
    app.settings.bank_sms_url = "https://test.example/bank-sms/blu"


async def test_sms_checkout_recovers_and_delivers_once_without_receipt(app: App) -> None:
    enable_bank_sms(app)
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    order = await order_by_card(buyer, store, "special_50")
    invoice = await app.one(select(BankInvoice).where(BankInvoice.order_id == order.id))
    moment = jdatetime.datetime.fromgregorian(datetime=datetime.now(ZoneInfo("Asia/Tehran")))
    text = (
        f"بلو\nواریز پول\nمشتری عزیز، {invoice.amount_toman * 10:,}ریال به حساب شما نشست.\n"
        f"موجودی: 9,999,999 ریال\n{moment:%H:%M}\n{moment:%Y.%m.%d}"
    )
    receiver = web.Application()
    mount_bank_sms(receiver, app.settings, app.session_factory)
    async with TestClient(TestServer(receiver)) as client:
        response = await client.post(
            "/bank-sms/blu",
            headers={"Authorization": "Bearer " + "x" * 40},
            json={"sender": "BluBank", "text": text},
        )
        assert response.status == 200
        assert await response.json() == {"outcome": "approved"}
    assert (await app.latest_order(buyer)).status == OrderStatus.PAYMENT_APPROVED
    await buyer.send_photo()
    assert buyer.last_text == texts.payment_already_confirmed
    async with app.session() as session:
        paid = await session.get(Order, order.id)
        paid.updated_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()
    mark = buyer.mark()
    await app.run_job("provision")
    await app.run_job("delivery")
    async with app.session() as session:
        assert (
            await receive_bank_sms(session, app.settings, text=text, sender="BluBank")
        ).outcome == "duplicate"
        await session.commit()
    await app.run_job("provision")
    await app.run_job("delivery")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert [message.kind for message in buyer.messages_since(mark)] == ["photo"]
    assert len(app.panel.users) == 1
    assert len(await app.all(select(Payment).where(Payment.order_id == order.id))) == 1
    entries = await app.all(
        select(WalletLedgerEntry).where(WalletLedgerEntry.reason == "bank_payment_suffix")
    )
    assert len(entries) == 1
    assert entries[0].amount_toman == invoice.suffix_toman


async def test_customer_can_cancel_open_order_from_payment_message(app: App) -> None:
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    order = await order_by_card(buyer, store, "special_50")
    await buyer.tap("لغو سفارش")
    assert (await app.latest_order(buyer)).status == OrderStatus.PENDING_PAYMENT
    assert "صبر کن" in buyer.last_text
    await buyer.tap("برگشت به پرداخت")
    assert (await app.latest_order(buyer)).status == OrderStatus.PENDING_PAYMENT
    await buyer.tap("لغو سفارش")
    await buyer.tap("واریز نکردم")
    assert (await app.latest_order(buyer)).status == OrderStatus.EXPIRED
    next_order = await order_by_card(buyer, store, "special_50")
    assert next_order.id != order.id


async def test_cancel_confirmation_cannot_close_order_after_receipt(app: App) -> None:
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    await order_by_card(buyer, store, "special_50")
    await buyer.tap("لغو سفارش")
    await buyer.send_photo()
    assert (await app.latest_order(buyer)).status == OrderStatus.PAYMENT_REVIEW
    await buyer.tap("واریز نکردم")
    assert (await app.latest_order(buyer)).status == OrderStatus.PAYMENT_REVIEW


async def test_admin_pauses_sms_and_buyer_can_submit_receipt(app: App) -> None:
    enable_bank_sms(app)
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    order = await order_by_card(buyer, store)
    invoice = await app.one(select(BankInvoice).where(BankInvoice.order_id == order.id))
    assert f"{invoice.suffix_toman * 10:,}".replace(",", "٬") + " ریال" in buyer.last_text
    assert "15 دقیقه" in buyer.last_text

    admin = app.admin(index=1)
    await admin.send("/admin")
    await admin.tap("فروشگاه")
    await admin.tap("💳 پرداخت")
    await admin.tap("خاموش کردن تأیید خودکار")
    assert "فقط دستی" in admin.last_text

    moment = jdatetime.datetime.fromgregorian(datetime=datetime.now(ZoneInfo("Asia/Tehran")))
    text = (
        f"بلو\nواریز پول\nمشتری عزیز، {invoice.amount_toman * 10:,}ریال به حساب شما نشست.\n"
        f"موجودی: 9,999,999 ریال\n{moment:%H:%M}\n{moment:%Y.%m.%d}"
    )
    async with app.session() as session:
        result = await receive_bank_sms(session, app.settings, text=text, sender="BluBank")
        await session.commit()
    assert result.outcome == "manual_review"
    assert (await app.latest_order(buyer)).status == OrderStatus.PENDING_PAYMENT

    manual_buyer = app.customer("reza")
    await manual_buyer.start()
    manual_order = await order_by_card(manual_buyer, store, name="reza12")
    assert not await app.all(select(BankInvoice).where(BankInvoice.order_id == manual_order.id))
    assert "بررسی دستی" in manual_buyer.last_text
    assert "15 دقیقه" not in manual_buyer.last_text
    await admin.tap("روشن کردن تأیید خودکار")
    async with app.session() as session:
        replay = await receive_bank_sms(session, app.settings, text=text, sender="BluBank")
        await session.commit()
    assert replay.outcome == "duplicate"
    await manual_buyer.tap_reply("🛒 خرید اشتراک")
    assert not await app.all(select(BankInvoice).where(BankInvoice.order_id == manual_order.id))

    auto_buyer = app.customer("sara")
    await auto_buyer.start()
    auto_order = await order_by_card(auto_buyer, store, name="sara12")
    assert await app.one(select(BankInvoice).where(BankInvoice.order_id == auto_order.id))

    await buyer.tap_reply("💬 پشتیبانی")
    await buyer.tap("ارسال رسید برای بررسی دستی")
    await buyer.send_photo()
    assert (await app.latest_order(buyer)).status == OrderStatus.PAYMENT_REVIEW
    await admin.tap("✅ تایید")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
