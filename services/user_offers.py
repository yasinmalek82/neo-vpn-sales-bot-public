"""Personal offers: a discount for one user, with no code.

An offer row is written with a marketing nudge (``write_offer``, inside the
transaction that claims the notice) and read by checkout (``best_live_offer``).
The order that uses it takes it with ``attach_offer``, a guarded UPDATE in the
order's own transaction, so two orders can never both spend one offer.

* **Live** means: not voided, not expired, and not held by a live order. An order
  that is dead (``DEAD_ORDER_STATUSES``) frees its offer again until the offer's
  own ``expires_at``; nothing rewrites ``order_id`` when an order dies. A
  ``PROVISION_FAILED`` order still holds it: the money was taken.
* **``min_volume_gb``** (the upgrade offer) is satisfied only by an order with
  strictly more volume. An unlimited order does not satisfy it.
* The queries live here and checkout only calls them
  (``services/checkout.py::personal_extra``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from database.models import DEAD_ORDER_STATUSES, Order, UserOffer
from services.ux_format import to_persian_digits


class OfferNoLongerLive(Exception):
    """The offer was spent, voided or expired between the summary and the order."""


@dataclass(frozen=True)
class OfferSpec:
    """What a nudge carries: the campaign's settings, plus the upgrade's volume floor."""

    campaign: str
    percent: int
    hours: int
    min_volume_gb: int | None = None


@dataclass(frozen=True)
class LiveOffer:
    id: int
    percent: int


def offer_line(spec: OfferSpec) -> str:
    """The one line a nudge gains when it carries an offer. Digits are Persian."""
    return texts.notify_offer_line.format(
        hours=to_persian_digits(spec.hours), percent=to_persian_digits(spec.percent)
    )


def _live_clause(now: datetime) -> ColumnElement[bool]:
    dead_orders = select(Order.id).where(Order.status.in_(DEAD_ORDER_STATUSES))
    return and_(
        UserOffer.voided_at.is_(None),
        UserOffer.expires_at > now,
        or_(UserOffer.order_id.is_(None), UserOffer.order_id.in_(dead_orders)),
    )


async def write_offer(
    session: AsyncSession,
    *,
    user_id: int,
    event_key: str,
    spec: OfferSpec,
    now: datetime | None = None,
) -> None:
    """Write the offer of a notice, keyed by the notice's ``event_key``.

    A row that exists (a retry after a released claim) is reused: its ``expires_at``
    moves to the new send time plus the hours and its ``voided_at`` is cleared, because
    a released claim voids the offer (the message was not out) and the retry puts it
    out again. ``order_id`` is never cleared. Clearing is safe: a message refused for
    good is marked ``undelivered`` and is never claimed again (``_reclaimable`` and
    ``claim_queued`` in ``service_notices.py`` both exclude it), so the only way to
    reach a voided row here is a claim that was released or re-armed and is going out
    now. The caller commits, together with the claim.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(hours=spec.hours)
    existing = await session.scalar(select(UserOffer.id).where(UserOffer.event_key == event_key))
    if existing is None:
        row = UserOffer(
            user_id=user_id,
            campaign=spec.campaign,
            percent=spec.percent,
            min_volume_gb=spec.min_volume_gb,
            event_key=event_key,
            created_at=moment,
            expires_at=expires_at,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            return
        except IntegrityError:
            pass  # a concurrent writer of the same key got there first: reuse theirs
    await session.execute(
        update(UserOffer)
        .where(UserOffer.event_key == event_key)
        .values(expires_at=expires_at, voided_at=None)
    )


async def void_offer(session: AsyncSession, *, event_key: str, now: datetime | None = None) -> None:
    """The message is not out (refused for good, or the claim was released): the offer was never seen."""
    await session.execute(
        update(UserOffer)
        .where(UserOffer.event_key == event_key, UserOffer.voided_at.is_(None))
        .values(voided_at=now or datetime.now(UTC))
    )


async def unvoided_offer_exists(
    session: AsyncSession,
    *,
    user_id: int,
    campaign: str,
    event_key_like: str,
    except_event_key: str | None = None,
) -> bool:
    """Is there a not-voided offer of this campaign for this user whose key matches the LIKE pattern?

    Used or unused, expired or not: any row that was not voided counts, because it was
    promised once. ``except_event_key`` is the key being claimed right now, so a retry of
    the same notice finds its own row and is not blocked by it.
    """
    clauses = [
        UserOffer.user_id == user_id,
        UserOffer.campaign == campaign,
        UserOffer.voided_at.is_(None),
        UserOffer.event_key.like(event_key_like),
    ]
    if except_event_key is not None:
        clauses.append(UserOffer.event_key != except_event_key)
    found = await session.scalar(select(UserOffer.id).where(*clauses).limit(1))
    return found is not None


async def offer_is_spent(session: AsyncSession, *, event_key: str) -> bool:
    """Is the offer of this notice held by a live order?

    A warning can come back under the same key (a periodic reset keeps the limit and the
    expiry). ``write_offer`` would only move the expiry of a row an order already took, so
    the message would promise a discount that is gone.
    """
    dead_orders = select(Order.id).where(Order.status.in_(DEAD_ORDER_STATUSES))
    found = await session.scalar(
        select(UserOffer.id)
        .where(
            UserOffer.event_key == event_key,
            UserOffer.order_id.is_not(None),
            UserOffer.order_id.not_in(dead_orders),
        )
        .limit(1)
    )
    return found is not None


async def best_live_offer(
    session: AsyncSession,
    *,
    user_id: int,
    volume_gb: int | None,
    now: datetime | None = None,
) -> LiveOffer | None:
    """The user's biggest live offer this order may use, or None."""
    moment = now or datetime.now(UTC)
    volume_ok: ColumnElement[bool] = UserOffer.min_volume_gb.is_(None)
    if volume_gb is not None and int(volume_gb) > 0:
        volume_ok = or_(volume_ok, UserOffer.min_volume_gb < int(volume_gb))
    row = (
        await session.execute(
            select(UserOffer.id, UserOffer.percent)
            .where(UserOffer.user_id == user_id, _live_clause(moment), volume_ok)
            .order_by(UserOffer.percent.desc(), UserOffer.expires_at, UserOffer.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return LiveOffer(id=int(row[0]), percent=int(row[1]))


async def attach_offer(
    session: AsyncSession,
    *,
    offer_id: int,
    user_id: int,
    order_id: int,
    now: datetime | None = None,
) -> bool:
    """Give the offer to this order. False when it was taken, voided or expired meanwhile.

    One guarded UPDATE, so of two orders racing for one offer exactly one wins.
    """
    taken = await session.scalar(
        update(UserOffer)
        .where(
            UserOffer.id == offer_id,
            UserOffer.user_id == user_id,
            _live_clause(now or datetime.now(UTC)),
        )
        .values(order_id=order_id)
        .returning(UserOffer.id)
    )
    return taken is not None
