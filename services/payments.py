"""Card-to-card / wallet payment submit / approve / reject (idempotent)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Order, OrderStatus, Payment, PaymentStatus, User, WalletLedgerEntry
from database.models.bank_sms import BankInvoice, BankSmsEvent
from services.bank_invoices import card_due_toman
from services.finance import has_valid_checkout_debit_for_order, refund_checkout_wallet_portion
from services.order_transitions import assert_transition, assert_wallet_full_cover_transition


class PaymentError(Exception):
    """Business-rule failure for payment operations."""


_APPROVED_STATUSES = frozenset(
    {
        OrderStatus.PAYMENT_APPROVED,
        OrderStatus.PROVISIONING,
        OrderStatus.ACTIVE,
        OrderStatus.PROVISION_FAILED,
    }
)


async def _claim_review_status(
    session: AsyncSession,
    order: Order,
    *,
    new_status: OrderStatus,
    paid_at: datetime | None = None,
) -> bool:
    """Move PAYMENT_REVIEW to ``new_status`` only if that is still the stored status.

    A second decision that lost the race gets False and must not refund or report.
    SQLite aborts the open transaction when the other decision already committed;
    that becomes ``decision_conflict`` rather than a blind overwrite.
    """
    now = datetime.now(UTC)
    values: dict[str, object] = {"status": new_status, "updated_at": now}
    if paid_at is not None:
        values["paid_at"] = paid_at
    try:
        result = await session.execute(
            update(Order)
            .where(Order.id == order.id, Order.status == OrderStatus.PAYMENT_REVIEW)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        await session.rollback()
        raise PaymentError("decision_conflict") from exc
    if result.rowcount != 1:
        session.expire(order)
        await session.refresh(order)
        return False
    session.expire(order)
    await session.refresh(order)
    return True


async def _load_order(session: AsyncSession, order_id: int) -> Order:
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.payment), selectinload(Order.plan))
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise PaymentError("order_not_found")
    return order


async def _get_payment_for_order(session: AsyncSession, order_id: int) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.order_id == order_id))
    return result.scalar_one_or_none()


async def submit_card_receipt(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
    receipt_file_id: str,
    card_number_display: str,
    receipt_media_type: str = "photo",
) -> Payment:
    """Create pending Payment and move order PENDING_PAYMENT → PAYMENT_REVIEW.

    Idempotent: if already PAYMENT_REVIEW with a payment, return that payment.
    Unique ``payments.order_id`` reduces double-insert races.
    """
    order = await _load_order(session, order_id)
    if order.user_id != user_id:
        raise PaymentError("order_not_owned")

    existing = await _get_payment_for_order(session, order_id)
    if order.status == OrderStatus.PAYMENT_REVIEW and existing is not None:
        return existing

    if order.status != OrderStatus.PENDING_PAYMENT:
        raise PaymentError(f"invalid_status:{order.status.value}")

    if receipt_media_type not in {"photo", "document"}:
        raise PaymentError("invalid_receipt_media_type")

    assert_transition(order.status, OrderStatus.PAYMENT_REVIEW)

    payment = Payment(
        order_id=order.id,
        user_id=user_id,
        amount_toman=await card_due_toman(session, order),
        method="card_to_card",
        card_number=card_number_display or None,
        status=PaymentStatus.pending,
        receipt_file_id=receipt_file_id,
        receipt_media_type=receipt_media_type,
    )
    session.add(payment)
    order.status = OrderStatus.PAYMENT_REVIEW
    await session.flush()
    return payment


@dataclass(frozen=True)
class LateReceipt:
    """A card receipt that arrived after its order expired."""

    order_id: int
    user_id: int
    card_due_toman: int
    expired_at: datetime | None
    first: bool  # False when this order's late receipt was already reported


def late_receipt_stamp(order_id: int) -> str:
    """The ``stamp`` of the manual wallet credit that settles this receipt.

    ``adjust_wallet_manually`` keys the ledger on admin + customer + stamp, so a
    stamp that names the order makes a double tap one credit.
    """
    return f"late_receipt:{order_id}"


def late_receipt_order_id(stamp: str) -> int | None:
    """The order a manual-credit stamp names, or None for an ordinary stamp."""
    prefix = late_receipt_stamp(0)[:-1]
    tail = stamp[len(prefix) :] if stamp.startswith(prefix) else ""
    return int(tail) if tail.isdigit() else None


async def late_receipt_credited(session: AsyncSession, *, user_id: int, order_id: int) -> bool:
    """True when any admin already credited this order's late receipt.

    The ledger key carries the admin's id, so two admins holding the same receipt
    would each write a different key; the stamp is what they share.
    """
    key_like = f"admin_adjust:%:{int(user_id)}:{late_receipt_stamp(order_id)}"
    found = await session.scalar(
        select(WalletLedgerEntry.id).where(WalletLedgerEntry.idempotency_key.like(key_like))
    )
    return found is not None


async def record_late_receipt(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
) -> LateReceipt:
    """A receipt for an EXPIRED order: report it once, change nothing else.

    The order is **not** revived. Expiry already refunded its wallet portion and
    the state machine has no way out of EXPIRED, so this is a new money path only
    in the sense of telling a human: the owner decides, and pays it out through
    the manual wallet credit.
    """
    from services.report_events import late_receipt as late_receipt_report
    from services.report_outbox import enqueue_report

    order = await _load_order(session, order_id)
    if order.user_id != user_id:
        raise PaymentError("order_not_owned")
    if order.status != OrderStatus.EXPIRED:
        raise PaymentError(f"invalid_status:{order.status.value}")
    card_due = await card_due_toman(session, order)
    if card_due <= 0:
        raise PaymentError("no_card_due")

    buyer = await session.get(User, int(order.user_id))
    if buyer is None:
        raise PaymentError("order_not_owned")
    report = late_receipt_report(
        order_id=int(order.id),
        telegram_id=int(buyer.telegram_id),
        name=buyer.first_name or buyer.username,
        username=buyer.username,
        is_reseller=bool(buyer.is_reseller),
        amount_toman=card_due,
        expired_at=order.updated_at,
        link_user_id=int(buyer.id),
    )
    queued = await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )
    return LateReceipt(
        order_id=int(order.id),
        user_id=int(order.user_id),
        card_due_toman=card_due,
        expired_at=order.updated_at,
        first=queued is not None,
    )


async def approve_wallet_payment(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
) -> tuple[Order, Payment]:
    """Mark order paid fully via wallet (no card receipt). Idempotent.

    Requires PENDING_PAYMENT, zero card due, and a valid checkout debit for the
    applied wallet portion (when price > 0). Uses a gated transition — does not
    open general PENDING_PAYMENT→PAYMENT_APPROVED for card bypass.
    """
    order = await _load_order(session, order_id)
    if order.user_id != user_id:
        raise PaymentError("order_not_owned")

    existing = await _get_payment_for_order(session, order_id)
    if (
        existing is not None
        and existing.method == "wallet"
        and existing.status == PaymentStatus.verified
        and order.status
        in {
            OrderStatus.PAYMENT_APPROVED,
            OrderStatus.PROVISIONING,
            OrderStatus.ACTIVE,
            OrderStatus.PROVISION_FAILED,
        }
    ):
        return order, existing

    if order.status != OrderStatus.PENDING_PAYMENT:
        raise PaymentError(f"invalid_status:{order.status.value}")

    card_due = max(0, int(order.price_toman) - int(order.wallet_applied_toman or 0))
    if card_due > 0:
        raise PaymentError("wallet_not_full_cover")

    if int(order.price_toman) > 0:
        if int(order.wallet_applied_toman or 0) < int(order.price_toman):
            raise PaymentError("wallet_not_full_cover")
        if not await has_valid_checkout_debit_for_order(session, order):
            raise PaymentError("wallet_debit_missing")

    assert_wallet_full_cover_transition(order.status, OrderStatus.PAYMENT_APPROVED)

    payment = existing or Payment(
        order_id=order.id,
        user_id=user_id,
        amount_toman=0,
        method="wallet",
        status=PaymentStatus.verified,
        verified_at=datetime.now(UTC),
        note="wallet_full_cover",
    )
    if existing is None:
        session.add(payment)
    else:
        payment.method = "wallet"
        payment.status = PaymentStatus.verified
        payment.verified_at = datetime.now(UTC)
        payment.amount_toman = 0

    order.status = OrderStatus.PAYMENT_APPROVED
    order.paid_at = datetime.now(UTC)
    await session.flush()
    await _report_payment(session, order, payment, approved=True)
    return order, payment


async def approve_payment(
    session: AsyncSession,
    *,
    order_id: int,
    admin_user_id: int,
) -> tuple[Order, Payment]:
    """Verify payment and move order PAYMENT_REVIEW → PAYMENT_APPROVED.

    Duplicate approve is a no-op returning the existing order/payment.
    """
    order = await _load_order(session, order_id)
    payment = order.payment or await _get_payment_for_order(session, order_id)
    if payment is None:
        raise PaymentError("payment_missing")
    return await _approve_card_payment(session, order, payment, admin_user_id=admin_user_id)


async def approve_bank_notification(
    session: AsyncSession, *, order_id: int, event_key: str
) -> tuple[Order, Payment]:
    """Only a persisted, matched bank event may take the receipt-free path."""
    event = await session.get(BankSmsEvent, event_key)
    invoice = await session.get(BankInvoice, order_id)
    if (
        event is None
        or invoice is None
        or event.outcome != "matched"
        or event.order_id != order_id
        or event.account_key != invoice.account_key
        or event.amount_toman != invoice.amount_toman
    ):
        raise PaymentError("bank_event_not_matched")
    order = await _load_order(session, order_id)
    payment = await _get_payment_for_order(session, order_id)
    if order.status == OrderStatus.PENDING_PAYMENT and payment is None:
        claimed = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PENDING_PAYMENT)
            .values(status=OrderStatus.PAYMENT_REVIEW)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise PaymentError("decision_conflict")
        await session.refresh(order)
        payment = Payment(
            order_id=order_id,
            user_id=order.user_id,
            amount_toman=invoice.amount_toman,
            method="card_to_card",
            status=PaymentStatus.pending,
        )
        session.add(payment)
        await session.flush()
    if (
        payment is None
        or payment.status != PaymentStatus.pending
        or payment.method != "card_to_card"
        or payment.amount_toman != invoice.amount_toman
    ):
        raise PaymentError("bank_payment_conflict")
    payment.reference_id = event_key
    return await _approve_card_payment(session, order, payment, admin_user_id=None)


async def _approve_card_payment(
    session: AsyncSession, order: Order, payment: Payment, *, admin_user_id: int | None
) -> tuple[Order, Payment]:

    if order.status in _APPROVED_STATUSES and payment.status == PaymentStatus.verified:
        return order, payment

    if order.status != OrderStatus.PAYMENT_REVIEW:
        raise PaymentError(f"invalid_status:{order.status.value}")

    assert_transition(order.status, OrderStatus.PAYMENT_APPROVED)

    now = datetime.now(UTC)
    claimed = await _claim_review_status(
        session,
        order,
        new_status=OrderStatus.PAYMENT_APPROVED,
        paid_at=now,
    )
    if not claimed:
        await session.refresh(payment)
        if order.status in _APPROVED_STATUSES and payment.status == PaymentStatus.verified:
            return order, payment
        raise PaymentError(f"invalid_status:{order.status.value}")

    payment.status = PaymentStatus.verified
    payment.admin_id = admin_user_id
    payment.verified_at = now
    await session.flush()
    invoice = await session.get(BankInvoice, order.id)
    if invoice is not None:
        if payment.amount_toman != invoice.amount_toman:
            raise PaymentError("bank_invoice_amount_mismatch")
        from services.wallet import credit

        await credit(
            session,
            user_id=order.user_id,
            amount_toman=invoice.suffix_toman,
            reason="bank_payment_suffix",
            idempotency_key=f"bank_suffix:{order.id}",
            order_id=order.id,
        )
    await _report_payment(session, order, payment, approved=True)
    return order, payment


async def _report_payment(
    session: AsyncSession,
    order: Order,
    payment: Payment,
    *,
    approved: bool,
    reason: str | None = None,
) -> None:
    """Queue the payment report inside the approving transaction.

    Enqueued rather than sent: a report failure must not roll back a decision
    the admin already made about real money.
    """
    from database.models import User
    from services.report_events import payment_received, payment_rejected
    from services.report_outbox import enqueue_report

    buyer = await session.get(User, int(order.user_id))
    if buyer is None:
        return
    admin = await session.get(User, int(payment.admin_id)) if payment.admin_id else None
    buyer_name = buyer.first_name or buyer.username
    admin_telegram_id = int(admin.telegram_id) if admin else None
    admin_username = admin.username if admin else None
    if approved:
        report = payment_received(
            order_id=int(order.id),
            telegram_id=int(buyer.telegram_id),
            name=buyer_name,
            username=buyer.username,
            is_reseller=bool(buyer.is_reseller),
            amount_toman=int(payment.amount_toman),
            method=str(getattr(payment.method, "value", payment.method)),
            admin_telegram_id=admin_telegram_id,
            admin_username=admin_username,
            when=payment.verified_at,
            link_user_id=int(buyer.id),
        )
    else:
        report = payment_rejected(
            order_id=int(order.id),
            telegram_id=int(buyer.telegram_id),
            name=buyer_name,
            username=buyer.username,
            is_reseller=bool(buyer.is_reseller),
            reason=(reason or "—")[:200],
            admin_telegram_id=admin_telegram_id,
            admin_username=admin_username,
            link_user_id=int(buyer.id),
        )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def reject_payment(
    session: AsyncSession,
    *,
    order_id: int,
    admin_user_id: int,
    note: str | None = None,
) -> tuple[Order, Payment]:
    """Reject payment and move order PAYMENT_REVIEW → REJECTED. Idempotent.

    Refunds any wallet checkout portion in the same transaction (exact + idempotent).
    """
    order = await _load_order(session, order_id)
    payment = order.payment or await _get_payment_for_order(session, order_id)
    if payment is None:
        raise PaymentError("payment_missing")

    if order.status == OrderStatus.REJECTED and payment.status == PaymentStatus.rejected:
        # Ensure refund idempotency even on legacy rows rejected before refund landing.
        await refund_checkout_wallet_portion(session, order=order)
        await session.flush()
        return order, payment

    if order.status != OrderStatus.PAYMENT_REVIEW:
        raise PaymentError(f"invalid_status:{order.status.value}")

    assert_transition(order.status, OrderStatus.REJECTED)

    claimed = await _claim_review_status(session, order, new_status=OrderStatus.REJECTED)
    if not claimed:
        await session.refresh(payment)
        if order.status == OrderStatus.REJECTED and payment.status == PaymentStatus.rejected:
            await refund_checkout_wallet_portion(session, order=order)
            await session.flush()
            return order, payment
        raise PaymentError(f"invalid_status:{order.status.value}")

    await refund_checkout_wallet_portion(session, order=order)

    payment.status = PaymentStatus.rejected
    payment.admin_id = admin_user_id
    if note:
        payment.note = note
    await session.flush()
    await _report_payment(session, order, payment, approved=False, reason=note)
    return order, payment
