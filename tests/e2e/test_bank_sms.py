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


async def test_sms_checkout_recovers_and_delivers_once_without_receipt(app: App) -> None:
    app.settings.bank_sms_enabled = True
    app.settings.scheduler_enabled = True
    app.settings.bank_sms_token = "x" * 40
    app.settings.bank_sms_sender = "BluBank"
    app.settings.bank_sms_account_key = "blu-test"
    app.settings.bank_sms_url = "https://test.example/bank-sms/blu"
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
