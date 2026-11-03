"""Valid order status transitions."""

from __future__ import annotations

from database.models.order import OrderStatus

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset(
        {OrderStatus.PAYMENT_REVIEW, OrderStatus.REJECTED, OrderStatus.EXPIRED}
    ),
    OrderStatus.PAYMENT_REVIEW: frozenset({OrderStatus.PAYMENT_APPROVED, OrderStatus.REJECTED}),
    OrderStatus.PAYMENT_APPROVED: frozenset({OrderStatus.PROVISIONING}),
    OrderStatus.PROVISIONING: frozenset({OrderStatus.ACTIVE, OrderStatus.PROVISION_FAILED}),
    OrderStatus.PROVISION_FAILED: frozenset({OrderStatus.PROVISIONING}),
    OrderStatus.ACTIVE: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}

# Wallet-full cover is intentionally NOT in ALLOWED_TRANSITIONS (would open card-bypass).
# Only approve_wallet_payment may use assert_wallet_full_cover_transition after debit checks.
WALLET_FULL_COVER_FROM = OrderStatus.PENDING_PAYMENT
WALLET_FULL_COVER_TO = OrderStatus.PAYMENT_APPROVED


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    if not can_transition(current, target):
        raise ValueError(f"invalid_order_transition:{current.value}->{target.value}")


def assert_wallet_full_cover_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Allow PENDING_PAYMENT→PAYMENT_APPROVED only via the dedicated wallet-full path.

    Callers must already have verified: full wallet cover + valid checkout debit for the order.
    This does not widen general ALLOWED_TRANSITIONS (card path still requires PAYMENT_REVIEW).
    """
    if current != WALLET_FULL_COVER_FROM or target != WALLET_FULL_COVER_TO:
        raise ValueError(f"invalid_wallet_full_cover_transition:{current.value}->{target.value}")
