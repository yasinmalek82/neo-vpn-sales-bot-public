"""Paid intents recover without another customer tap, debit or panel account."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from database.models import Order, OrderStatus, WalletEntryType, WalletLedgerEntry
from tests.e2e.harness import App, browse_to_plan, publish_store


async def pending_wallet_checkout(app: App, *, applied: bool = False):
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    await app.give_wallet(buyer, 300_000)
    app.panel.timeout_next("POST /api/user", applied=applied)
    await browse_to_plan(buyer, store)
    await buyer.send("ali12")
    await buyer.tap("بدون کد تخفیف")
    await buyer.tap("ثبت سفارش")
    order = await app.latest_order(buyer)
    assert order.status == OrderStatus.PROVISIONING
    async with app.session() as session:
        pending = await session.get(Order, order.id)
        pending.created_at = datetime.now(UTC) - timedelta(minutes=20)
        pending.paid_at = pending.created_at
        pending.updated_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()
    return buyer, order


@pytest.mark.parametrize("applied", [False, True])
async def test_scheduler_resumes_lost_create_reply_without_new_payment(
    app: App, applied: bool
) -> None:
    buyer, order = await pending_wallet_checkout(app, applied=applied)
    mark = buyer.mark()
    await app.run_job("provision")
    await app.run_job("delivery")
    await app.run_job("provision")
    await app.run_job("delivery")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert [m.kind for m in buyer.messages_since(mark)] == ["photo"]
    assert len(app.panel.users) == 1
    assert len(await app.all(select(Order))) == 1
    debits = await app.all(
        select(WalletLedgerEntry).where(WalletLedgerEntry.entry_type == WalletEntryType.debit)
    )
    assert len(debits) == 1
    assert await app.wallet_balance(buyer) == 300_000 - order.price_toman


async def test_failed_recovery_backs_off_and_reports_aged_intent_without_refund(app: App) -> None:
    from database.models.report import ReportOutbox

    buyer, order = await pending_wallet_checkout(app)
    app.panel.fail_next("POST /api/user", status=500, times=None)
    await app.run_job("provision")
    before = len(app.panel.requests_to("POST /api/user"))
    await app.run_job("provision")
    assert len(app.panel.requests_to("POST /api/user")) == before
    assert (await app.latest_order(buyer)).status == OrderStatus.PROVISIONING
    assert await app.wallet_balance(buyer) == 300_000 - order.price_toman
    reports = await app.all(
        select(ReportOutbox).where(ReportOutbox.event_key == f"order:{order.id}:provision_aged")
    )
    assert len(reports) == 1
    assert reports[0].link_user_id == order.user_id


@pytest.mark.parametrize(
    "status", [OrderStatus.PROVISION_FAILED, OrderStatus.PAYMENT_REVIEW, OrderStatus.EXPIRED]
)
async def test_worker_does_not_retry_nonpending_orders_or_take_another_payment(
    app: App, status: OrderStatus
) -> None:
    buyer, order = await pending_wallet_checkout(app)
    async with app.session() as session:
        row = await session.get(Order, order.id)
        row.status = status
        row.updated_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()
    before = len(app.panel.requests)
    await app.run_job("provision")
    assert len(app.panel.requests) == before
    assert (await app.latest_order(buyer)).status == status
    assert await app.wallet_balance(buyer) == 300_000 - order.price_toman


async def test_terminal_recovery_error_keeps_paid_intent_for_operator_without_refund(
    app: App,
) -> None:
    from database.models.report import ReportOutbox

    buyer, order = await pending_wallet_checkout(app)
    app.panel.fail_next("POST /api/user", status=400)
    await app.run_job("provision")
    assert (await app.latest_order(buyer)).status == OrderStatus.PROVISIONING
    assert await app.wallet_balance(buyer) == 300_000 - order.price_toman
    assert len(app.panel.users) == 0
    assert (
        len(
            await app.all(
                select(ReportOutbox).where(
                    ReportOutbox.event_key == f"order:{order.id}:provision_aged"
                )
            )
        )
        == 1
    )
