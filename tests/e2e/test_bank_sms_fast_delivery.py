"""A committed bank approval can be delivered promptly without weakening recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from database.models import (
    BankInvoice,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    WalletLedgerEntry,
)
from database.models.bank_sms import BankSmsEvent
from database.models.provision_recovery import ProvisionRecovery
from services.bank_invoices import aware
from services.bank_sms import receive_bank_sms
from tests.e2e.harness import App, order_by_card, publish_store
from tests.e2e.test_bank_sms import enable_bank_sms
from web.bank_sms import mount_bank_sms


async def _new_checkout(app: App):
    enable_bank_sms(app)
    store = await publish_store(app)
    buyer = app.customer("fast_buyer")
    await buyer.start()
    order = await order_by_card(buyer, store, "special_50")
    invoice = await app.one(select(BankInvoice).where(BankInvoice.order_id == order.id))
    moment = jdatetime.datetime.fromgregorian(datetime=datetime.now(ZoneInfo("Asia/Tehran")))
    text = (
        f"بلو\nواریز پول\nمشتری عزیز، {invoice.amount_toman * 10:,}ریال به حساب شما نشست.\n"
        f"موجودی: 9,999,999 ریال\n{moment:%H:%M}\n{moment:%Y.%m.%d}"
    )
    return buyer, order, invoice, text


async def _post_sms(app: App, text: str) -> str:
    receiver = web.Application()
    mount_bank_sms(receiver, app.settings, app.session_factory)
    async with TestClient(TestServer(receiver)) as client:
        response = await client.post(
            "/bank-sms/blu",
            headers={"Authorization": "Bearer " + "x" * 40},
            json={"sender": "BluBank", "text": text},
        )
        assert response.status == 200
        return (await response.json())["outcome"]


async def test_fresh_committed_bank_payment_is_provisioned_and_delivered_once(app: App) -> None:
    buyer, order, invoice, text = await _new_checkout(app)
    mark = buyer.mark()
    assert await _post_sms(app, text) == "approved"
    assert (await app.latest_order(buyer)).status == OrderStatus.PAYMENT_APPROVED

    # No artificial change to updated_at and no sleep: the first worker pass is enough.
    await asyncio.gather(app.run_job("provision"), app.run_job("provision"))
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    await app.run_job("delivery")
    assert await _post_sms(app, text) == "duplicate"
    await app.run_job("provision")
    await app.run_job("delivery")

    assert [message.kind for message in buyer.messages_since(mark)] == ["photo"]
    assert len(app.panel.users) == 1
    assert len(app.panel.requests_to("POST /api/user")) == 1
    assert len(await app.all(select(Payment).where(Payment.order_id == order.id))) == 1
    entries = await app.all(
        select(WalletLedgerEntry).where(WalletLedgerEntry.reason == "bank_payment_suffix")
    )
    assert len(entries) == 1 and entries[0].amount_toman == invoice.suffix_toman


async def test_uncommitted_bank_approval_cannot_trigger_panel(app: App) -> None:
    buyer, _order, _invoice, text = await _new_checkout(app)
    async with app.session() as session:
        result = await receive_bank_sms(session, app.settings, text=text, sender="BluBank")
        assert result.outcome == "approved"
        before = len(app.panel.requests_to("POST /api/user"))
        await app.run_job("provision")
        assert len(app.panel.requests_to("POST /api/user")) == before
        await session.commit()
    await app.run_job("provision")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert len(app.panel.users) == 1


@pytest.mark.parametrize(
    "alteration", ["event", "event_order", "reference", "payment", "method", "provisioning"]
)
async def test_fast_path_requires_the_committed_matching_approved_payment(
    app: App, alteration: str
) -> None:
    buyer, order, _invoice, text = await _new_checkout(app)
    assert await _post_sms(app, text) == "approved"
    async with app.session() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
        event = await session.scalar(select(BankSmsEvent).where(BankSmsEvent.order_id == order.id))
        row = await session.get(Order, order.id)
        assert payment is not None and event is not None and row is not None
        if alteration == "event":
            event.outcome = "manual_review"
        elif alteration == "event_order":
            event.order_id = None
        elif alteration == "reference":
            payment.reference_id = "not-the-bank-event"
        elif alteration == "payment":
            payment.status = PaymentStatus.pending
        elif alteration == "method":
            payment.method = "wallet"
        else:
            row.status = OrderStatus.PROVISIONING
        await session.commit()

    expected_status = (await app.latest_order(buyer)).status
    before = len(app.panel.requests)
    await app.run_job("provision")
    assert len(app.panel.requests) == before
    assert (await app.latest_order(buyer)).status == expected_status


async def test_fast_failure_keeps_backoff_and_recovers_without_duplicate_service(app: App) -> None:
    buyer, order, _invoice, text = await _new_checkout(app)
    assert await _post_sms(app, text) == "approved"
    app.panel.timeout_next("POST /api/user", applied=True)

    await app.run_job("provision")
    first_attempts = len(app.panel.requests_to("POST /api/user"))
    assert first_attempts == 1
    assert len(app.panel.users) == 1
    assert (await app.latest_order(buyer)).status == OrderStatus.PROVISIONING
    await app.run_job("provision")
    assert len(app.panel.requests_to("POST /api/user")) == first_attempts

    async with app.session() as session:
        recovery = await session.get(ProvisionRecovery, order.id)
        pending = await session.get(Order, order.id)
        assert recovery is not None and pending is not None
        assert aware(recovery.next_attempt_at) > datetime.now(UTC)
        recovery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        pending.updated_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()

    await app.run_job("provision")
    await app.run_job("delivery")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert len(app.panel.users) == 1
    assert len(app.panel.requests_to("POST /api/user")) == first_attempts
    assert len(await app.all(select(Payment).where(Payment.order_id == order.id))) == 1
