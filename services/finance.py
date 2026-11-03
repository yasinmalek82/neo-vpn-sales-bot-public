"""Shared financial rules for wallet checkout apply / refund (idempotent).

Handlers and automation must call these helpers instead of duplicating ledger logic.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.exceptions import PasarGuardAPIError, PasarGuardAuthError, PasarGuardNotFoundError
from database.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    User,
    WalletEntryType,
    WalletLedgerEntry,
)
from services.wallet import (
    REASON_CHECKOUT_DEBIT,
    REASON_PROVISION_FAIL_CREDIT,
    checkout_debit_key,
    credit,
    credit_provision_failed,
    debit,
)

REASON_CHECKOUT_REFUND = "checkout_refund"


def is_terminal_wallet_provision_rejection(exc: BaseException) -> bool:
    """Whether the panel proved it rejected a create request before success.

    The documented create endpoint rejections are deliberately narrow.  A
    timeout, conflict, 5xx or local error may follow a successful panel write,
    so it cannot authorize a wallet credit.
    """
    if isinstance(exc, PasarGuardAuthError):
        return exc.status_code == 401
    if isinstance(exc, PasarGuardNotFoundError):
        # The panel answered «no such user» to a renew/switch read: it proved
        # nothing was written, the service is simply gone.
        return exc.status_code == 404
    if isinstance(exc, PasarGuardAPIError):
        return exc.status_code in {400, 422}
    return False


async def settle_wallet_provision_failure(  # noqa: PLR0911 - explicit credit admission gates
    session: AsyncSession,
    *,
    order_id: int,
    original_exception: BaseException,
    max_n: int,
) -> int:
    """Credit a proven wallet checkout rejection once, without committing.

    This admission gate intentionally requires both sides of a settled wallet
    checkout: the verified wallet Payment and its exact checkout ledger debit.
    Caller owns the transaction and UI.  Returning zero means no new credit
    was created (including an idempotent repeat).
    """
    if max_n < 1 or not is_terminal_wallet_provision_rejection(original_exception):
        return 0

    order = await session.get(Order, order_id)
    if order is None or order.status is not OrderStatus.PROVISION_FAILED:
        return 0
    amount = int(order.price_toman)
    if amount <= 0 or int(order.wallet_applied_toman or 0) != amount:
        return 0

    payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
    if (
        payment is None
        or payment.user_id != order.user_id
        or payment.method != "wallet"
        or payment.status is not PaymentStatus.verified
    ):
        return 0

    debit_entry = await session.scalar(
        select(WalletLedgerEntry).where(
            WalletLedgerEntry.idempotency_key == checkout_debit_key(order.id),
            WalletLedgerEntry.entry_type == WalletEntryType.debit,
            WalletLedgerEntry.reason == REASON_CHECKOUT_DEBIT,
            WalletLedgerEntry.user_id == order.user_id,
            WalletLedgerEntry.order_id == order.id,
            WalletLedgerEntry.amount_toman == amount,
        )
    )
    if debit_entry is None:
        return 0

    entry, created = await credit_provision_failed(
        session,
        user_id=order.user_id,
        order_id=order.id,
        amount_toman=amount,
        max_n=max_n,
    )
    if entry is None or not created:
        return 0
    return int(entry.amount_toman)


async def park_unconfirmed_wallet_provision(
    session: AsyncSession,
    *,
    order_id: int,
    original_exception: BaseException,
) -> int | None:
    """Keep a wallet checkout whose panel outcome is unknown, and tell the owner.

    Call it **after** the caller's rollback, when ``settle_wallet_provision_failure``
    returned zero. A timeout or a 5xx may follow a panel write that succeeded, so
    nothing is refunded; the order stays at its committed PROVISIONING intent,
    where the same panel username resumes it.

    Returns the wallet amount that was taken, so the customer can be told what
    happened to their money, or ``None`` when the rollback also took the order
    and the debit with it (the failure came before the intent was committed, so
    nothing was charged). The owner's report is queued and committed here: the
    rollback discarded the ``_mark_provision_failed`` report.
    """
    from services.report_events import provision_failed as build_failure
    from services.report_outbox import enqueue_report

    order = await session.get(Order, order_id)
    if order is None or order.status is not OrderStatus.PROVISIONING:
        return None
    amount = int(order.wallet_applied_toman or 0)
    if amount <= 0:
        return None
    buyer = await session.get(User, int(order.user_id))
    raw = str(original_exception).strip() or original_exception.__class__.__name__
    report = build_failure(
        order_id=order_id,
        telegram_id=int(buyer.telegram_id) if buyer else 0,
        detail=raw[:200],
        username=buyer.username if buyer else None,
        panel_username=order.panel_username,
        link_user_id=int(buyer.id) if buyer else None,
    )
    # Not the order's `provision_failed` key: a later, proven failure of this
    # same order must still reach the owner.
    await enqueue_report(
        session,
        event_key=f"order:{order_id}:provision_unconfirmed",
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )
    await session.commit()
    return amount


async def find_unfinished_wallet_order(session: AsyncSession, user_id: int) -> Order | None:
    """The customer's wallet-paid order that was debited but never delivered.

    PROVISIONING after a panel failure with no proof either way. Starting the
    purchase again must continue this order, not debit a second time: the
    one-open-order index covers only the two payment states, so nothing else
    stops a second PROVISIONING order.
    """
    candidates = (
        await session.scalars(
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.status == OrderStatus.PROVISIONING,
                Order.trial_kind.is_(None),
                Order.price_toman > 0,
            )
            .order_by(Order.id.desc())
        )
    ).all()
    for order in candidates:
        if int(order.wallet_applied_toman or 0) != int(order.price_toman):
            continue
        payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
        if (
            payment is not None
            and payment.method == "wallet"
            and payment.status is PaymentStatus.verified
            and await has_valid_checkout_debit_for_order(session, order)
        ):
            return order
    return None


async def settle_reseller_trial_provision_failure(
    session: AsyncSession,
    *,
    order_id: int,
    original_exception: BaseException | None,
    max_n: int,
) -> int:
    """Refund one proved terminal rejection of a paid reseller giveaway.

    A reseller trial has no wallet ``Payment`` row or checkout debit.  Its
    durable financial proof is instead the order-specific ``reseller_trial``
    debit created immediately after the giveaway order.  The credit still uses
    the common provision-failure credit key so retry admission remains one
    symmetric protocol.
    """
    if (
        max_n < 1
        or original_exception is None
        or not is_terminal_wallet_provision_rejection(original_exception)
    ):
        return 0

    order = await session.get(Order, order_id)
    if order is None or order.status is not OrderStatus.PROVISION_FAILED:
        return 0
    amount = int(order.price_toman)
    if amount <= 0 or order.trial_kind != "reseller":
        return 0

    if not await has_valid_reseller_trial_debit(session, order):
        return 0

    entry, created = await credit_provision_failed(
        session,
        user_id=order.user_id,
        order_id=order.id,
        amount_toman=amount,
        max_n=max_n,
    )
    if entry is None or not created:
        return 0
    assert entry.reason == REASON_PROVISION_FAIL_CREDIT
    return int(entry.amount_toman)


async def has_valid_reseller_trial_debit(session: AsyncSession, order: Order) -> bool:
    """Validate the original paid-giveaway debit for settlement and recovery."""
    amount = int(order.price_toman)
    if amount <= 0 or order.trial_kind != "reseller":
        return False
    debit_entry = await session.scalar(
        select(WalletLedgerEntry).where(
            WalletLedgerEntry.idempotency_key == f"reseller_trial:{order.id}",
            WalletLedgerEntry.entry_type == WalletEntryType.debit,
            WalletLedgerEntry.reason == "reseller_trial",
            WalletLedgerEntry.user_id == order.user_id,
            WalletLedgerEntry.order_id == order.id,
            WalletLedgerEntry.amount_toman == amount,
        )
    )
    return debit_entry is not None


def checkout_refund_key(order_id: int) -> str:
    return f"checkout_refund:{order_id}"


async def get_checkout_debit(session: AsyncSession, order_id: int) -> WalletLedgerEntry | None:
    result = await session.execute(
        select(WalletLedgerEntry).where(
            WalletLedgerEntry.idempotency_key == checkout_debit_key(order_id)
        )
    )
    return result.scalar_one_or_none()


async def has_valid_checkout_debit_for_order(session: AsyncSession, order: Order) -> bool:
    """True when wallet_applied is covered by an idempotent checkout debit for this order."""
    applied = max(0, int(order.wallet_applied_toman or 0))
    if applied <= 0:
        return True
    entry = await get_checkout_debit(session, order.id)
    if entry is None:
        return False
    if entry.reason != REASON_CHECKOUT_DEBIT:
        return False
    return int(entry.amount_toman) == applied and entry.user_id == order.user_id


async def apply_checkout_debit(
    session: AsyncSession,
    *,
    user_id: int,
    order: Order,
) -> tuple[WalletLedgerEntry | None, bool]:
    """Debit ``order.wallet_applied_toman`` at checkout. Idempotent. No-op if zero."""
    amount = max(0, int(order.wallet_applied_toman or 0))
    if amount <= 0:
        return None, False
    return await debit(
        session,
        user_id=user_id,
        amount_toman=amount,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
        note="checkout_apply",
    )


async def refund_checkout_wallet_portion(
    session: AsyncSession,
    *,
    order: Order,
) -> tuple[WalletLedgerEntry | None, bool]:
    """Refund wallet portion on expire/reject. Exact amount, idempotent, same txn as caller.

    Credits ``order.wallet_applied_toman`` when a checkout debit exists (or amount > 0).
    Safe to call repeatedly; second call returns existing credit entry.
    """
    amount = max(0, int(order.wallet_applied_toman or 0))
    if amount <= 0:
        return None, False
    return await credit(
        session,
        user_id=order.user_id,
        amount_toman=amount,
        reason=REASON_CHECKOUT_REFUND,
        idempotency_key=checkout_refund_key(order.id),
        order_id=order.id,
        note="checkout_refund_on_cancel",
    )
