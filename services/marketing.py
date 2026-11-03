"""Who may be nudged, and how often.

One function, ``may_nudge``, answers «may this user and this service get a
marketing message» and every campaign calls it. A nudge goes to a user only when

* the user is not a reseller and not banned;
* no order that references the service carries a ``customer_label`` (a user whose
  reseller flag was removed still owns walk-in customers' services);
* for the trial nudge, the trial order's ``trial_kind`` is personal.

``nudged_within_cap`` is the other half: at most one capped marketing nudge per
user per 24 hours, counted over delivered notices. The cart reminder is exempt
and does not count; lifecycle notices are not marketing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, PasarGuardAccount, User
from database.models.lifecycle import ServiceNotice
from database.models.marketing import CAPPED_NOTICE_KINDS
from database.models.order import TRIAL_KIND_PERSONAL
from services.orders import user_has_open_order

NUDGE_CAP = timedelta(hours=24)

# The windows the campaigns look at (the admin screen quotes them from here).
CART_WINDOW_MINUTES = 15  # the reminder goes out in an order's last N minutes
TRIAL_CONVERT_MIN_HOURS = 24  # a trial ended at least this long ago ...
TRIAL_CONVERT_MAX_DAYS = 7  # ... and less than this many days ago
WINBACK_MIN_DAYS = 5  # a paid service ended at least this long ago ...
WINBACK_MAX_DAYS = 30  # ... and at most this many days ago
WINBACK_REPEAT_DAYS = 90  # at most one winback per user in this many days
UPGRADE_MIN_TIME_LEFT_PERCENT = 40  # volume running out with at least this much time left


async def _carries_a_customer_label(
    session: AsyncSession, *, account_id: int | None, order_id: int | None
) -> bool | None:
    """True when an order of the service or the order itself has a label; None for an unknown account."""
    references = []
    if account_id is not None:
        account = await session.get(PasarGuardAccount, account_id)
        if account is None:
            return None
        references.append(
            or_(
                Order.panel_username == account.panel_username,
                Order.renew_account_id == account.id,
            )
        )
    if order_id is not None:
        references.append(Order.id == order_id)
    if not references:
        return False
    labelled = await session.scalar(
        select(Order.id).where(or_(*references), Order.customer_label.is_not(None)).limit(1)
    )
    return labelled is not None


async def may_nudge(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int | None = None,
    order_id: int | None = None,
    require_personal_trial: bool = False,
    require_no_open_order: bool = False,
) -> bool:
    """May this user get a marketing message about this service or order?

    ``account_id`` names a live service (every order that references it is
    checked for a customer label); ``order_id`` names the order the message is
    about (checked too, and for the trial nudge it must be a personal trial).
    ``require_no_open_order`` is for the messages that may carry a personal offer: a
    customer with an open order is taken back to it at its old price, so an offer
    would be promised and could not be used. Not for the cart reminder, which is
    about that very order.
    """
    user = await session.get(User, user_id)
    if user is None or user.is_reseller or user.is_banned:
        return False
    if require_no_open_order and await user_has_open_order(session, user_id):
        return False
    if (
        await _carries_a_customer_label(session, account_id=account_id, order_id=order_id)
        is not False
    ):
        return False
    if not require_personal_trial:
        return True
    if order_id is None:
        return False
    kind = await session.scalar(select(Order.trial_kind).where(Order.id == order_id))
    return kind == TRIAL_KIND_PERSONAL


async def nudged_within_cap(
    session: AsyncSession, *, user_id: int, now: datetime | None = None
) -> bool:
    """True when this user already got a capped marketing nudge in the last 24 hours."""
    cutoff = (now or datetime.now(UTC)) - NUDGE_CAP
    found = await session.scalar(
        select(ServiceNotice.id)
        .where(
            ServiceNotice.user_id == user_id,
            ServiceNotice.kind.in_(CAPPED_NOTICE_KINDS),
            ServiceNotice.delivered_at.is_not(None),
            ServiceNotice.undelivered.is_(False),
            ServiceNotice.delivered_at > cutoff,
        )
        .limit(1)
    )
    return found is not None
