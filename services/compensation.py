"""What the unused days of a service are worth, for a manual wallet credit.

The owner's policy (2026-09-26): when direct stops working on a customer's
ISP and support cannot fix it, the unused days go to their wallet as credit
toward the tunnel product. The bot works out the figure; the owner credits it
with the existing wallet button. No new money path — only a number to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, PasarGuardAccount

_DAY = 86_400


@dataclass(frozen=True)
class UnusedValue:
    amount_toman: int
    paid_toman: int
    period_days: int
    days_left: int


def compute_unused_value(
    *,
    paid_toman: int,
    period_days: int,
    expire_unix: int | None,
    now_unix: int,
    paid_until_unix: int | None = None,
) -> UnusedValue | None:
    """``paid × days_left ÷ period``, in whole toman and whole days, rounded down.

    Days left are capped at one period: after an early renew the expiry covers
    more than the last payment, and this figure only speaks for that payment.
    ``paid_until_unix`` (the expiry the paid order bought) also caps
    the end: days an admin gifted with ``extend_days`` push the expiry past what
    was paid for, and must not be valued at the paid rate.
    ``None`` when there is nothing to value — free, no expiry, or already over.
    """
    if paid_toman <= 0 or period_days <= 0 or not expire_unix:
        return None
    end_unix = expire_unix if paid_until_unix is None else min(expire_unix, paid_until_unix)
    days_left = min((end_unix - now_unix) // _DAY, period_days)
    if days_left <= 0:
        return None
    return UnusedValue(
        amount_toman=paid_toman * days_left // period_days,
        paid_toman=paid_toman,
        period_days=period_days,
        days_left=days_left,
    )


async def unused_value_for_account(
    session: AsyncSession,
    account: PasarGuardAccount,
    *,
    expire_unix: int | None,
    now: datetime | None = None,
) -> UnusedValue | None:
    """Value the unused days against the last paid order for this service."""
    order = await session.scalar(
        select(Order)
        .where(
            Order.panel_username == account.panel_username,
            Order.user_id == account.user_id,
            Order.status == OrderStatus.ACTIVE,
            Order.price_toman > 0,
            Order.delivery_duration_days.is_not(None),
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    if order is None:
        return None
    period_days = int(order.delivery_duration_days or 0)
    # What this payment bought, as an absolute expiry. A renewal stamps it
    # (`renew_target_expire_unix`) and it starts at the OLD expiry, not at
    # activation, so `activated_at + period` would undervalue an early renewal.
    # A new service has no target: its paid period starts when it was activated.
    activated = order.activated_at
    if order.renew_target_expire_unix:  # 0 = a renew with no expiry
        paid_until: int | None = int(order.renew_target_expire_unix)
    elif activated is not None:
        paid_until = (
            int(activated.replace(tzinfo=activated.tzinfo or UTC).timestamp()) + period_days * _DAY
        )
    else:
        paid_until = None
    return compute_unused_value(
        paid_until_unix=paid_until,
        paid_toman=int(order.price_toman),
        period_days=int(order.delivery_duration_days or 0),
        expire_unix=expire_unix,
        now_unix=int((now or datetime.now(UTC)).timestamp()),
    )
