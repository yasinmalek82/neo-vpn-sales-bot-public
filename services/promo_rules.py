"""Who may use a promo code now: the four usage rules, decided before a quote.

``check_promo_eligibility`` is called from ``checkout.resolve_price`` (non-reseller path, only
when a code was typed), so the summary and the till refuse alike. It reads the rules from the
pricing document and counts live redemptions (``services/promo_redemptions.py``). The limits
are counted again inside the order's own transaction (``create_pending_order``), so two
customers cannot both take the last use. Both counts start at the code's ``counted_from``
(the moment it was created), so a code deleted and created again starts from zero.

It says nothing about a code it does not find or finds inactive: ``pricing._apply_promo``
stays the one place that refuses those. An old-shaped entry carries no rule.

Order of checks: expired, first purchase only, per-user limit, total limit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from services.orders import user_has_successful_paid_order
from services.promo_redemptions import (
    PromoExpired,
    PromoFirstPurchaseOnly,
    PromoRuleError,
    PromoRules,
    PromoUsedUp,
    PromoUserLimit,
    count_live_redemptions,
    rules_from_entry,
)

__all__ = [
    "PromoExpired",
    "PromoFirstPurchaseOnly",
    "PromoRuleError",
    "PromoRules",
    "PromoUsedUp",
    "PromoUserLimit",
    "check_promo_eligibility",
]


async def check_promo_eligibility(
    session: AsyncSession,
    cfg: dict[str, Any],
    promo_code: str,
    user: User,
    *,
    now: datetime | None = None,
) -> PromoRules | None:
    """Raise the ``PromoRuleError`` of the first rule that refuses ``user``.

    Returns the code's rules (so the caller can stamp the quote), or ``None`` when the code is
    unknown or inactive: pricing refuses those itself.
    """
    code = promo_code.strip().upper()
    entry = (cfg.get("promo_codes") or {}).get(code)
    if entry is None or (isinstance(entry, dict) and not bool(entry.get("active", True))):
        return None
    rules = rules_from_entry(entry)
    moment = now or datetime.now(UTC)
    if rules.expires_at is not None and moment >= rules.expires_at:
        raise PromoExpired()
    if rules.first_purchase_only and await user_has_successful_paid_order(session, user.id):
        raise PromoFirstPurchaseOnly()
    if rules.per_user_limit > 0 and (
        await count_live_redemptions(
            session, code, user_id=user.id, counted_from=rules.counted_from
        )
        >= rules.per_user_limit
    ):
        raise PromoUserLimit()
    if (
        rules.total_limit > 0
        and await count_live_redemptions(session, code, counted_from=rules.counted_from)
        >= rules.total_limit
    ):
        raise PromoUsedUp()
    return rules
