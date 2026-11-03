"""The price a checkout summary and the till both charge.

The summary screen and order confirmation used to apply the fixed-price lock
separately. The order copy once had no reseller exception, so a shop saw the
agreed price and was then charged retail. ``quote_checkout`` is that decision.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from services import report_events
from services.orders import user_has_successful_paid_order
from services.pricing import (
    PlanType,
    PriceResult,
    _apply_promo,
    calculate_fixed_price,
    calculate_fixed_price_with_promo,
    calculate_price,
)
from services.promo_rules import PromoRules, check_promo_eligibility
from services.report_outbox import enqueue_report
from services.reseller import quote_custom_volume, quote_ready_plan
from services.store_catalog import flat_plan_list_enabled, ready_package_purchase_payload
from services.store_config import get_published_store_config, load_pricing_config_async
from services.store_pricing import DEFAULT_TIER, duration_from_package_options, get_price
from services.user_offers import best_live_offer
from services.ux_format import to_persian_digits


async def referred_extra(session: AsyncSession, settings: Any, db_user: User | None) -> int:
    if db_user is None or db_user.referred_by_id is None:
        return 0
    if await user_has_successful_paid_order(session, db_user.id):
        return 0
    return int(settings.referral_referred_discount_percent)


@dataclass(frozen=True)
class PersonalExtra:
    """The per-user extra percent, and the offer that supplied it (None: the referral did)."""

    percent: int
    offer_id: int | None = None


async def personal_extra(
    session: AsyncSession, settings: Any, data: dict[str, Any], db_user: User | None
) -> PersonalExtra:
    """``max(referral extra, the user's best live offer for this order)``, in ONE place.

    Both price paths (``resolve_price`` and the fixed-price lock in ``quote_checkout``)
    ask here, so the summary and the till cannot disagree. A reseller gets nothing: an
    agreed price replaces retail, and a percent on top of it would undercut the owner.
    On a tie the referral wins and the offer stays unused. The percent then goes through
    ``forbid_extra_discount``, ``max_discount_percent`` and ``discount_combine`` exactly as
    the referral percent always did.
    """
    if db_user is None or getattr(db_user, "is_reseller", False):
        return PersonalExtra(0)
    referral = await referred_extra(session, settings, db_user)
    offer = await best_live_offer(session, user_id=db_user.id, volume_gb=data.get("volume_gb"))
    if offer is not None and offer.percent > referral:
        return PersonalExtra(offer.percent, offer.id)
    return PersonalExtra(referral)


def _offer_was_applied(price: PriceResult, extra: PersonalExtra, data: dict[str, Any]) -> bool:
    """Did the engine actually take the offer's percent? Read from what pricing reported.

    No when the referral supplied the extra, the plan forbids extra discounts, the buyer
    ended up on a reseller price, or a promo code won the best-of comparison (a tie goes to
    the code). Under ``sum`` both apply, so the offer does. A ``max_discount_percent`` cap
    still counts as applied.
    """
    if extra.offer_id is None or data.get("forbid_extra_discount"):
        return False
    if price.meta.get("pricing_mode") == "reseller":
        return False
    promo = price.meta.get("promo_code")
    if promo and price.meta.get("discount_combine") == "best_of_promo_vs_extra":
        return Decimal(str(price.meta.get("promo_percent") or 0)) < Decimal(extra.percent)
    return True


def with_offer(price: PriceResult, extra: PersonalExtra, data: dict[str, Any]) -> PriceResult:
    """Stamp ``user_offer_id`` / ``user_offer_percent`` on the quote, only when it was applied."""
    if not _offer_was_applied(price, extra, data):
        return price
    meta = dict(price.meta)
    meta["user_offer_id"] = extra.offer_id
    meta["user_offer_percent"] = extra.percent
    return PriceResult(
        original_price=price.original_price,
        discount_amount=price.discount_amount,
        final_price=price.final_price,
        currency=price.currency,
        meta=meta,
    )


def _promo_was_applied(price: PriceResult, extra: PersonalExtra) -> bool:
    """Did the engine actually take the promo code's percent? Read from what pricing reported.

    The mirror of ``_offer_was_applied``: under ``best_of_promo_vs_extra`` the code counts when its
    percent is at least the extra percent (a tie goes to the code), so exactly one of the code and
    the offer is applied; under ``sum`` both are. A reseller price never carries one. A
    ``max_discount_percent`` cap still counts as applied.
    """
    if not price.meta.get("promo_code") or price.meta.get("pricing_mode") == "reseller":
        return False
    if price.meta.get("discount_combine") == "best_of_promo_vs_extra":
        return Decimal(str(price.meta.get("promo_percent") or 0)) >= Decimal(extra.percent)
    return True


def with_promo_redemption(
    price: PriceResult, extra: PersonalExtra, rules: PromoRules | None
) -> PriceResult:
    """Stamp ``promo_redeem`` on the quote, only when the code was applied to this order.

    ``create_pending_order`` writes the redemption row from it and counts the limits again. A
    code that lost the best-of comparison to a bigger offer or referral uses nothing up.
    """
    if rules is None or not _promo_was_applied(price, extra):
        return price
    meta = dict(price.meta)
    meta["promo_redeem"] = {
        "code": str(price.meta["promo_code"]).strip().upper(),
        "per_user_limit": rules.per_user_limit,
        "total_limit": rules.total_limit,
        "counted_from": rules.counted_from.isoformat() if rules.counted_from else None,
    }
    return PriceResult(
        original_price=price.original_price,
        discount_amount=price.discount_amount,
        final_price=price.final_price,
        currency=price.currency,
        meta=meta,
    )


def price_from_fixed(
    data: dict[str, Any],
    *,
    promo_code: str | None,
    extra_discount_percent: int,
    max_discount_percent: int | None,
    currency: str = "IRT",
) -> PriceResult:
    """Adapt ready-package FSM data to the pure fixed-price service."""
    forbid = bool(data.get("forbid_extra_discount"))
    meta: dict[str, Any] = {
        "pricing_mode": "fixed_ready",
        "store_package_id": data.get("store_package_id"),
        "volume_gb": data.get("volume_gb"),
        "duration_days": data.get("duration_days"),
        "concurrent_users": data.get("concurrent_users"),
        "duration_months": data.get("duration_months"),
    }
    if not forbid and promo_code:
        meta["promo_code"] = promo_code
    return calculate_fixed_price(
        price_toman=int(data["fixed_price_toman"]),
        extra_discount_percent=extra_discount_percent,
        max_discount_percent=max_discount_percent,
        forbid_extra_discount=forbid,
        currency=currency,
        metadata=meta,
    )


async def apply_reseller_price(
    session: AsyncSession,
    cfg: dict[str, Any],
    data: dict[str, Any],
    retail: PriceResult,
    db_user: User | None,
) -> PriceResult:
    """Swap in the agreed reseller price, if this buyer is a reseller.

    The agreed price **replaces** the retail one rather than discounting it.
    Stacking a referral or promo percent on top of a negotiated wholesale price
    would let a shop buy below what the owner agreed to — the discount was
    already taken when the price was set.

    Anything with no reseller shape — an unlimited custom build, say — stays at
    retail. That is the honest outcome: the owner agreed rates for volume and
    fixed prices for ready plans, and nothing else.
    """
    if db_user is None or not getattr(db_user, "is_reseller", False):
        return retail

    slug = data.get("store_package_slug")
    if slug:
        if "sel_duration_days" in data:
            duration_days = data.get("sel_duration_days")
        elif "delivery_duration_days" in data:
            duration_days = data.get("delivery_duration_days")
        else:
            duration_days = data.get("duration_days")
        quote = await quote_ready_plan(
            session,
            user_id=db_user.id,
            package_slug=str(slug),
            retail_price_toman=int(retail.final_price),
            duration_days=duration_days,
            tier=str(data.get("sel_tier") or DEFAULT_TIER),
        )
        what = str(data.get("title_fa") or slug)
    elif (
        PlanType(data["plan_type"]) is PlanType.VOLUME
        and data.get("volume_gb")
        and data.get("duration_days")
    ):
        quote = await quote_custom_volume(
            session,
            cfg,
            user_id=db_user.id,
            volume_gb=int(data["volume_gb"]),
            duration_days=int(data["duration_days"]),
        )
        what = (
            f"{to_persian_digits(int(data['volume_gb']))} گیگ / "
            f"{to_persian_digits(int(data['duration_days']))} روز"
        )
    else:
        return retail

    if quote.crossed_over:
        # The shop is charged the lower price and never notices; the owner has a
        # rate that stops earning somewhere and this is the only moment it shows.
        report = report_events.reseller_price_crossed_over(
            telegram_id=int(db_user.telegram_id),
            name=db_user.first_name or db_user.username,
            username=db_user.username,
            what=what,
            reseller_price_toman=quote.reseller_price_toman,
            retail_price_toman=quote.retail_price_toman,
            link_user_id=int(db_user.id),
        )
        with contextlib.suppress(Exception):
            await enqueue_report(
                session,
                event_key=report.event_key,
                topic_key=report.topic_key,
                body=report.body,
                link_user_id=report.link_user_id,
            )

    meta = dict(retail.meta)
    meta.update(
        {
            "pricing_mode": "reseller",
            "retail_price_toman": quote.retail_price_toman,
            "reseller_price_toman": quote.reseller_price_toman,
            "reseller_crossed_over": quote.crossed_over,
        }
    )
    return PriceResult(
        original_price=quote.retail_price_toman,
        discount_amount=max(0, quote.retail_price_toman - quote.price_toman),
        final_price=quote.price_toman,
        currency=retail.currency,
        meta=meta,
    )


def should_lock_to_fixed_price(
    data: dict[str, Any], price: PriceResult, *, promo_code: str | None
) -> bool:
    """Should the engine's answer be replaced by the plan's own fixed price?

    Yes for a ready plan bought without a promo code — that is what «قیمت ثابت»
    means. **Never** for a reseller: their agreed price is the answer, and
    ``price_from_fixed`` knows only the retail number.

    This lived twice, inline, in the summary and in order creation. The order
    copy had no reseller exception and rewrote unconditionally, so every
    reseller paid full retail on every ready plan while each screen before it
    showed the agreed price. ``quote_checkout`` is the only rewrite.
    """
    if data.get("fixed_price_toman") is None:
        return False
    if promo_code:
        return False
    return price.meta.get("pricing_mode") != "reseller"


async def resolve_price(
    session: AsyncSession,
    settings: Any,
    data: dict[str, Any],
    *,
    promo_code: str | None,
    db_user: User | None,
) -> PriceResult:
    cfg = await load_pricing_config_async(session, settings=settings)
    plan_type = PlanType(data["plan_type"])
    # A reseller's agreed cell or per-gig rate replaces retail. Feeding a promo
    # or a referral percent into that comparison let min() charge below the
    # number the owner already set.
    promo_rules: PromoRules | None = None
    if db_user is not None and getattr(db_user, "is_reseller", False):
        promo_code = None
        personal = PersonalExtra(0)
    else:
        # After the reseller drop above, so a shop is never checked or refused. Before pricing,
        # so the summary and the till refuse alike. An unknown or inactive code passes through
        # silently: ``_apply_promo`` refuses it.
        if promo_code and db_user is not None:
            promo_rules = await check_promo_eligibility(session, cfg, promo_code, db_user)
        personal = await personal_extra(session, settings, data, db_user)
    extra = personal.percent
    if data.get("fixed_price_toman") is not None and not promo_code:
        retail = price_from_fixed(
            data,
            promo_code=None,
            extra_discount_percent=extra,
            max_discount_percent=settings.max_discount_percent,
            currency=str(cfg.get("currency") or "IRT"),
        )
        return with_offer(
            await apply_reseller_price(session, cfg, data, retail, db_user), personal, data
        )
    if (
        data.get("fixed_price_toman") is not None
        and promo_code
        and data.get("forbid_extra_discount")
    ):
        # The plan takes no extra discount, but the code still has to be a
        # real one: an unknown code used to pass silently here while every
        # other plan refused it. A real code is refused for THIS plan.
        _apply_promo(
            cfg,
            original=Decimal("0"),
            duration_discount_percent=Decimal("0"),
            promo_code=promo_code,
        )
        raise ValueError("promo_not_applicable")
    if data.get("fixed_price_toman") is not None and promo_code:
        retail = calculate_fixed_price_with_promo(
            cfg,
            price_toman=int(data["fixed_price_toman"]),
            promo_code=promo_code,
            extra_discount_percent=extra,
            max_discount_percent=settings.max_discount_percent,
            currency=str(cfg.get("currency") or "IRT"),
            metadata={
                "pricing_mode": "fixed_ready",
                "store_package_id": data.get("store_package_id"),
                "volume_gb": data.get("volume_gb"),
                "duration_days": data.get("duration_days"),
                "concurrent_users": data.get("concurrent_users"),
                "duration_months": data.get("duration_months"),
            },
        )
        return with_promo_redemption(
            with_offer(
                await apply_reseller_price(session, cfg, data, retail, db_user), personal, data
            ),
            personal,
            promo_rules,
        )
    retail = calculate_price(
        cfg,
        plan_type=plan_type,
        volume_gb=data.get("volume_gb"),
        duration_days=data.get("duration_days"),
        concurrent_users=data.get("concurrent_users"),
        duration_months=data.get("duration_months"),
        promo_code=promo_code,
        extra_discount_percent=extra,
        max_discount_percent=settings.max_discount_percent,
    )
    return with_promo_redemption(
        with_offer(await apply_reseller_price(session, cfg, data, retail, db_user), personal, data),
        personal,
        promo_rules,
    )


async def quote_checkout(
    session: AsyncSession,
    settings: Any,
    data: dict[str, Any],
    *,
    promo_code: str | None,
    db_user: User | None,
) -> PriceResult:
    """Price the summary shows and the till charges.

    When the lock applies, the plan's fixed price replaces the engine. A
    reseller quote is left as resolved: the lock does not know the agreed price.
    """
    price = await resolve_price(session, settings, data, promo_code=promo_code, db_user=db_user)
    if not should_lock_to_fixed_price(data, price, promo_code=promo_code):
        return price
    personal = await personal_extra(session, settings, data, db_user)
    return with_offer(
        price_from_fixed(
            data,
            promo_code=None,
            extra_discount_percent=personal.percent,
            max_discount_percent=settings.max_discount_percent,
            currency=price.currency,
        ),
        personal,
        data,
    )


class CheckoutUnavailable(ValueError):
    """The selected ready plan or one of its sale terms was withdrawn."""


async def refresh_ready_checkout(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ready purchase by live slug, retaining explicit account intent.

    FSM prices and row IDs are presentation snapshots. They cannot authorize a
    new order after publication. Custom purchases have no ready-package slug.
    """
    slug = data.get("store_package_slug")
    if not slug:
        return dict(data)
    cfg = await get_published_store_config(session)
    package = next((p for p in cfg.packages if p.slug == slug), None) if cfg else None
    if cfg is None or package is None or not package.enabled:
        raise CheckoutUnavailable
    renewing = bool(data.get("renew_account_id")) and not data.get("renew_resets")
    permission = "allow_renew" if renewing else "allow_new_sale"
    if not getattr(package, permission):
        raise CheckoutUnavailable
    by_id = {int(c.id): c for c in cfg.categories}
    category = by_id.get(int(package.category_id))
    current = category
    seen: set[int] = set()
    while current is not None:
        if int(current.id) in seen or not current.enabled:
            raise CheckoutUnavailable
        seen.add(int(current.id))
        if not dict(current.sales_flags or {}).get(permission, True):
            raise CheckoutUnavailable
        if current.parent_id is None:
            break
        current = by_id.get(int(current.parent_id))
    if category is None or current is None:
        raise CheckoutUnavailable
    flat = flat_plan_list_enabled(category)
    days = (
        duration_from_package_options(dict(package.options or {}))
        if flat
        else data.get("sel_duration_days", data.get("duration_days"))
    )
    tier = DEFAULT_TIER if flat else str(data.get("sel_tier") or DEFAULT_TIER)
    cell = get_price(list(cfg.prices), slug=str(slug), duration_days=days, tier=tier)
    if cell is None:
        raise CheckoutUnavailable
    payload = ready_package_purchase_payload(
        package,
        list(cfg.prices),
        duration_days=days,
        tier=tier,
        use_selected_duration=not flat,
    )
    # Remove dimensions belonging to a former product type. Preserve account
    # identity and reset/switch intent; a catalog edit never chooses those.
    refreshed = {
        **data,
        **dict.fromkeys(
            ("volume_gb", "duration_days", "duration_months", "concurrent_users", "hwid_limit")
        ),
        **payload,
    }
    if payload.get("volume_gb") is not None:
        kind = PlanType.VOLUME.value
    elif payload.get("concurrent_users") is not None:
        kind = PlanType.UNLIMITED.value
    else:
        raise CheckoutUnavailable
    if renewing and data.get("hwid_limit") is not None:
        refreshed["hwid_limit"] = data["hwid_limit"]
    refreshed.update(
        plan_type=kind,
        store_category_id=int(category.id),
        store_category_slug=str(category.slug),
        sel_duration_days=days,
        sel_tier=tier,
        fixed_price_toman=int(cell),
    )
    return refreshed


async def checkout_delivery_terms(
    session: AsyncSession, settings: Any, data: dict[str, Any]
) -> dict[str, Any] | None:
    """Comparable consent snapshot; row IDs are deliberately excluded.

    An extension retains its service's panel groups and HWID policy. New and
    switched services follow the current store groups and require consent if
    those groups change even when the price stays the same.
    """
    if not data.get("store_package_slug"):
        return None
    from services.panel_settings import resolve_groups_for_plan_type

    groups = await resolve_groups_for_plan_type(
        session,
        settings,
        data["plan_type"],
        store_category_id=data.get("store_category_id"),
        tier=str(data.get("sel_tier") or DEFAULT_TIER),
    )
    renewing = bool(data.get("renew_account_id")) and not data.get("renew_resets")
    days = data.get("duration_days")
    if days is None and data.get("duration_months") is not None:
        days = int(data["duration_months"]) * 30
    return {
        **{
            key: data.get(key)
            for key in (
                "plan_type",
                "volume_gb",
                "concurrent_users",
                "hwid_limit",
            )
        },
        "duration_days": days,
        "group_ids": None if renewing else sorted(groups),
    }
