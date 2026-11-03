"""Promo code usage: the rules a code carries, the rows that count, the recheck.

``services/promo_rules.py`` decides eligibility from the pricing document before a quote
(the summary and the till agree); ``create_pending_order`` calls ``record_redemption`` in the
order's own savepoint, which writes the row and counts again, so two customers cannot both take
the last use. This module holds what both need and imports nothing from ``orders.py``, so the
order writer can import it at module level.

* **Identity is the code string**, upper-cased as ``pricing._apply_promo`` does, and a code
  counts its uses **from the moment it was created**: its entry carries ``counted_from`` (an
  ISO date-time, stamped by ``upsert_draft_promo`` when the code is not already in the draft),
  and only redemptions written at or after it count. Deleting a code and creating it again
  starts from zero; editing a code that exists keeps its count. An entry with no readable
  ``counted_from`` (every code made before this, the bare shape) counts every redemption.
* **Live** means the redemption's order is not in ``DEAD_ORDER_STATUSES``. An unpaid order holds
  its place; an expired or rejected one gives it back. Nothing rewrites a row when an order dies.
* **Old entries keep working**: the bare ``"10"`` and ``{"percent", "active"}`` shapes carry no
  rule. A rule value that cannot be read (not a non-negative int, an unparsable date) reads
  as absent, the same as a missing key; the store admin refuses to write one
  (``services/store_strategy.py``), so only a hand-edited document can contain it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import DEAD_ORDER_STATUSES, Order, PromoRedemption

RULE_KEYS = ("first_purchase_only", "per_user_limit", "total_limit", "expires_at")
# Not a rule the owner sets: a stamp written when the code is created (see the module docstring).
COUNTED_FROM_KEY = "counted_from"


class PromoRuleError(ValueError):
    """A code exists and is active, but a usage rule refuses this buyer.

    ``code`` is stable and is also the exception's text; it is never shown to a customer.
    """

    code: ClassVar[str] = "promo_rule"

    def __init__(self) -> None:
        super().__init__(self.code)


class PromoExpired(PromoRuleError):
    code = "promo_expired"


class PromoUsedUp(PromoRuleError):
    """The total limit is reached."""

    code = "promo_used_up"


class PromoFirstPurchaseOnly(PromoRuleError):
    code = "promo_first_only"


class PromoUserLimit(PromoRuleError):
    code = "promo_user_limit"


@dataclass(frozen=True)
class PromoRules:
    """What the owner set on a code. The defaults are «no limit».

    ``counted_from`` is not a rule: it is the stamp of the moment the code was created, read
    from the entry and never written by the wizard (``store_strategy._rule_keys`` leaves it out).
    """

    first_purchase_only: bool = False
    per_user_limit: int = 0
    total_limit: int = 0
    expires_at: datetime | None = None
    counted_from: datetime | None = None


def _limit(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def parse_expiry(value: Any) -> datetime | None:
    """An ISO date-time as an aware datetime; naive reads as UTC; unreadable reads as none."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def rules_from_entry(entry: Any) -> PromoRules:
    """The rules of one ``promo_codes`` entry, whatever shape the entry has."""
    if not isinstance(entry, dict):
        return PromoRules()
    return PromoRules(
        first_purchase_only=entry.get("first_purchase_only") is True,
        per_user_limit=_limit(entry.get("per_user_limit")),
        total_limit=_limit(entry.get("total_limit")),
        expires_at=parse_expiry(entry.get("expires_at")),
        counted_from=parse_expiry(entry.get(COUNTED_FROM_KEY)),
    )


def _live_count(code: str, user_id: int | None, counted_from: datetime | None) -> Any:
    stmt = (
        select(func.count(PromoRedemption.id))
        .join(Order, Order.id == PromoRedemption.order_id)
        .where(PromoRedemption.code == code, Order.status.not_in(DEAD_ORDER_STATUSES))
    )
    if user_id is not None:
        stmt = stmt.where(PromoRedemption.user_id == user_id)
    if counted_from is not None:
        # Rows hold UTC; an offset in a hand-written stamp is converted, never compared raw.
        stmt = stmt.where(PromoRedemption.created_at >= counted_from.astimezone(UTC))
    return stmt


async def count_live_redemptions(
    session: AsyncSession,
    code: str,
    *,
    user_id: int | None = None,
    counted_from: datetime | None = None,
) -> int:
    """Live uses of ``code``, by everyone or (``user_id``) by one buyer.

    With ``counted_from`` only the uses written at or after that moment count: this is the one
    definition of «live count» that the quote, the till and the admin list all use.
    """
    return int(await session.scalar(_live_count(code.strip().upper(), user_id, counted_from)) or 0)


async def live_usage_by_code(
    session: AsyncSession, since: Mapping[str, datetime | None] | None = None
) -> dict[str, int]:
    """Live uses per code, for the admin list. Keyed on the code string, unused codes left out.

    ``since`` maps each listed code to its ``counted_from`` (or None) and asks about exactly
    those codes; without it, every code that has a redemption is asked, counting all of them.
    Each answer comes from ``count_live_redemptions``, so the list shows the number the checks use.
    """
    if since is None:
        codes = list(await session.scalars(select(PromoRedemption.code).distinct()))
        since = {str(code): None for code in codes}
    usage: dict[str, int] = {}
    for code, counted_from in since.items():
        used = await count_live_redemptions(session, code, counted_from=counted_from)
        if used:
            usage[code.strip().upper()] = used
    return usage


async def record_redemption(
    session: AsyncSession,
    *,
    code: str,
    user_id: int,
    order_id: int,
    per_user_limit: int,
    total_limit: int,
    counted_from: datetime | None = None,
) -> None:
    """Write the redemption of a just-flushed order, then count again.

    Called inside the order's savepoint. The recount includes this very row (its order is
    live), so a limit of N passes the Nth use and refuses the N+1st, and a loser of two racing
    writers sees the winner's row and raises, which rolls the savepoint back: neither the
    order nor the redemption exists. Raises ``PromoUserLimit`` before ``PromoUsedUp``. The
    recount leaves out uses before ``counted_from`` (the moment the code was created).
    """
    clean = code.strip().upper()
    session.add(PromoRedemption(code=clean, user_id=user_id, order_id=order_id))
    await session.flush()
    if per_user_limit > 0 and await count_live_redemptions(
        session, clean, user_id=user_id, counted_from=counted_from
    ) > (per_user_limit):
        raise PromoUserLimit()
    if (
        total_limit > 0
        and await count_live_redemptions(session, clean, counted_from=counted_from) > total_limit
    ):
        raise PromoUsedUp()
