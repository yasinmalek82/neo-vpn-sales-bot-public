"""Real payment/ledger/outbox writes, durable replays and invoice ownership."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bot.config import Settings
from database.models import Order, OrderStatus, Payment, PaymentStatus, WalletLedgerEntry
from tests.unit.test_payments import _seed

NOW = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def sms(amount: int, *, clock: str = "17:30") -> str:
    return f"بلو\nواریز پول\nمشتری عزیز، {amount * 10:,}ریال به حساب شما نشست.\nموجودی: 9,999,999 ریال\n{clock}\n۱۴۰۵.۰۸.۱۱"


def config(**overrides):
    return Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        **{
            "BANK_SMS_ENABLED": True,
            "SCHEDULER_ENABLED": True,
            "BANK_SMS_TOKEN": "x" * 40,
            "BANK_SMS_SENDER": "BluBank",
            "BANK_SMS_ACCOUNT_KEY": "blu-main",
            "BANK_SMS_URL": "https://pay.example.com/bank-sms/blu",
            "PAYMENT_CARD_NUMBER": "6219861234567890",
            **overrides,
        },
    )


async def seed(factory):
    async with factory() as session:
        customer, admin, order = await _seed(session)
        order.created_at = NOW - timedelta(minutes=2)
        await session.commit()
        return customer.id, admin.id, order.id


async def reserve(factory, order_id, **kwargs):
    from services.bank_sms import reserve_bank_invoice

    async with factory() as session:
        invoice = await reserve_bank_invoice(
            session, config(), order_id=order_id, now=NOW - timedelta(minutes=1), **kwargs
        )
        await session.commit()
        return invoice


async def accept(factory, amount, **kwargs):
    from services.bank_sms import receive_bank_sms

    async with factory() as session:
        result = await receive_bank_sms(
            session, config(), text=sms(amount), sender="BluBank", now=NOW, **kwargs
        )
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_exact_credit_approves_without_photo_and_replay_never_credits_twice(two_sessions):
    from database.models.bank_sms import BankSmsEvent

    factory, _ = two_sessions
    customer_id, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    assert 150_000 < invoice.amount_toman <= 150_999
    result = await accept(factory, invoice.amount_toman)
    assert result.outcome == "approved"
    assert (await accept(factory, invoice.amount_toman)).outcome == "duplicate"
    async with factory() as session:
        order = await session.get(Order, order_id)
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        assert order.status == OrderStatus.PAYMENT_APPROVED
        assert order.price_toman == 150_000
        assert payment.status == PaymentStatus.verified
        assert payment.amount_toman == invoice.amount_toman
        assert payment.receipt_file_id is None and payment.admin_id is None
        entries = (
            await session.scalars(
                select(WalletLedgerEntry).where(WalletLedgerEntry.user_id == customer_id)
            )
        ).all()
        assert len(entries) == 1
        assert entries[0].amount_toman == invoice.amount_toman - 150_000
        assert await session.scalar(select(func.count()).select_from(BankSmsEvent)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.EXPIRED, OrderStatus.REJECTED])
async def test_late_or_cancelled_invoice_never_activates(two_sessions, status):
    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        order = await session.get(Order, order_id)
        order.status = status
        await session.commit()
    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0


@pytest.mark.asyncio
async def test_wrong_amount_is_not_applied_to_closest_invoice(two_sessions):
    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    await reserve(factory, order_id)
    assert (await accept(factory, 150_000)).outcome == "manual_review"


@pytest.mark.asyncio
async def test_manual_approval_uses_reserved_amount_and_returns_suffix_once(two_sessions):
    from services.payments import approve_payment, submit_card_receipt

    factory, _ = two_sessions
    customer_id, admin_id, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        payment = await submit_card_receipt(
            session,
            order_id=order_id,
            user_id=customer_id,
            receipt_file_id="receipt",
            card_number_display="6219861234567890",
        )
        assert payment.amount_toman == invoice.amount_toman
        await approve_payment(session, order_id=order_id, admin_user_id=admin_id)
        await approve_payment(session, order_id=order_id, admin_user_id=admin_id)
        await session.commit()
    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 1


@pytest.mark.asyncio
async def test_disabled_reserves_nothing(two_sessions):
    from services.bank_sms import reserve_bank_invoice

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    async with factory() as session:
        assert (
            await reserve_bank_invoice(session, config(BANK_SMS_ENABLED=False), order_id=order_id)
            is None
        )


@pytest.mark.asyncio
async def test_expired_reservation_does_not_match_even_if_scheduler_has_not_run(two_sessions):
    from database.models.bank_sms import BankInvoice

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        stored = await session.get(BankInvoice, order_id)
        stored.expires_at = NOW - timedelta(minutes=1)
        await session.commit()
    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"


@pytest.mark.asyncio
async def test_expiry_cannot_overwrite_bank_approval_selected_before_it_committed(
    two_sessions, monkeypatch
):
    from services.automation import expire_stale_orders

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        execute = session.execute
        intercepted = False

        async def interleave(statement, *args, **kwargs):
            nonlocal intercepted
            result = await execute(statement, *args, **kwargs)
            if not intercepted and getattr(statement, "is_select", False):
                intercepted = True
                # Release SQLite's snapshot to model PostgreSQL READ COMMITTED:
                # the already-selected ORM rows remain stale after another commit.
                await session.commit()
                assert (await accept(factory, invoice.amount_toman)).outcome == "approved"
            return result

        monkeypatch.setattr(session, "execute", interleave)
        expired = await expire_stale_orders(session, expiry_minutes=1, now=NOW)
        await session.commit()
        assert expired == []
    async with factory() as session:
        assert (await session.get(Order, order_id)).status == OrderStatus.PAYMENT_APPROVED


@pytest.mark.asyncio
async def test_receipt_in_review_can_be_approved_by_bank_without_duplicate_payment(two_sessions):
    from services.payments import submit_card_receipt

    factory, _ = two_sessions
    user_id, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        await submit_card_receipt(
            session,
            order_id=order_id,
            user_id=user_id,
            receipt_file_id="photo",
            card_number_display="6219861234567890",
        )
        await session.commit()
    assert (await accept(factory, invoice.amount_toman)).outcome == "approved"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_other_card_stale_or_future_bank_time_never_approves(two_sessions):
    from services.bank_sms import receive_bank_sms

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    for settings, message in [
        (config(PAYMENT_CARD_NUMBER="6219861234567891"), sms(invoice.amount_toman)),
        (config(), sms(invoice.amount_toman, clock="17:28")),
        (config(), sms(invoice.amount_toman, clock="18:30")),
    ]:
        async with factory() as session:
            result = await receive_bank_sms(
                session, settings, text=message, sender="BluBank", now=NOW
            )
            assert result.outcome == "manual_review"
            await session.rollback()


@pytest.mark.asyncio
async def test_reservation_is_stable_and_never_reused_after_expiry(two_sessions):
    from database.models import User
    from services.bank_invoices import reserve_bank_invoice

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    again = await reserve(factory, order_id)
    assert again.amount_toman == invoice.amount_toman
    async with factory() as session:
        order = await session.get(Order, order_id)
        order.status = OrderStatus.EXPIRED
        user = User(telegram_id=222, referral_code="other-customer")
        session.add(user)
        await session.flush()
        # Different base price whose preferred suffix lands on the old total.
        other = Order(
            user_id=user.id,
            plan_id=order.plan_id,
            price_toman=invoice.amount_toman - 3,
            status=OrderStatus.PENDING_PAYMENT,
            created_at=NOW - timedelta(minutes=2),
        )
        session.add(other)
        await session.flush()
        second = await reserve_bank_invoice(session, config(), order_id=other.id, now=NOW)
        assert second.amount_toman != invoice.amount_toman
        await session.commit()


@pytest.mark.asyncio
async def test_missing_wallet_debit_cannot_be_covered_by_matching_card_sms(two_sessions):
    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    async with factory() as session:
        order = await session.get(Order, order_id)
        order.wallet_applied_toman = 50_000
        await session.commit()
    invoice = await reserve(factory, order_id)
    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"


@pytest.mark.asyncio
async def test_outer_rollback_removes_bank_event_approval_and_wallet_credit(two_sessions):
    from database.models.bank_sms import BankSmsEvent
    from services.bank_sms import receive_bank_sms

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        result = await receive_bank_sms(
            session, config(), text=sms(invoice.amount_toman), sender="BluBank", now=NOW
        )
        assert result.outcome == "approved"
        await session.rollback()
    async with factory() as session:
        assert (await session.get(Order, order_id)).status == OrderStatus.PENDING_PAYMENT
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0
        assert await session.scalar(select(func.count()).select_from(BankSmsEvent)) == 0
    assert (await accept(factory, invoice.amount_toman)).outcome == "approved"


@pytest.mark.asyncio
async def test_customer_cancel_releases_checkout_and_keeps_late_sms_for_review(two_sessions):
    from services.finance import apply_checkout_debit
    from services.orders import get_open_order
    from services.payments import cancel_unpaid_order
    from services.wallet import credit, get_balance

    factory, _ = two_sessions
    customer_id, other_id, order_id = await seed(factory)
    async with factory() as session:
        await credit(
            session,
            user_id=customer_id,
            amount_toman=50_000,
            reason="manual_adjustment",
            idempotency_key="qa-credit",
        )
        order = await session.get(Order, order_id)
        order.wallet_applied_toman = 50_000
        await apply_checkout_debit(session, user_id=customer_id, order=order)
        await session.commit()
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        assert not await cancel_unpaid_order(session, order_id=order_id, user_id=other_id)
        assert await cancel_unpaid_order(session, order_id=order_id, user_id=customer_id)
        await session.commit()
    async with factory() as session:
        assert await get_open_order(session, customer_id) is None
        assert await get_balance(session, customer_id) == 50_000
        assert not await cancel_unpaid_order(session, order_id=order_id, user_id=customer_id)
    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"


@pytest.mark.asyncio
async def test_manual_order_with_same_amount_makes_existing_auto_invoice_ambiguous(two_sessions):
    from database.models import User
    from services.bank_invoices import reserve_bank_invoice
    from services.payment_settings import set_bank_sms_auto

    factory, _ = two_sessions
    _, admin_id, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    async with factory() as session:
        await set_bank_sms_auto(session, False, admin_user_id=admin_id)
        order = await session.get(Order, order_id)
        buyer = User(telegram_id=222, referral_code="manual-customer")
        session.add(buyer)
        await session.flush()
        manual = Order(
            user_id=buyer.id,
            plan_id=order.plan_id,
            price_toman=invoice.amount_toman,
            status=OrderStatus.PENDING_PAYMENT,
            created_at=NOW - timedelta(minutes=1),
        )
        session.add(manual)
        await session.flush()
        assert await reserve_bank_invoice(session, config(), order_id=manual.id, now=NOW) is None
        await session.commit()
    async with factory() as session:
        await set_bank_sms_auto(session, True, admin_user_id=admin_id)
        await session.commit()

    assert (await accept(factory, invoice.amount_toman)).outcome == "manual_review"
    async with factory() as session:
        assert (await session.get(Order, order_id)).status == OrderStatus.PENDING_PAYMENT
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
