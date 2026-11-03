"""Referral stats, invite links, and idempotent reward recording."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, ReferralLog, ReferralStatus, User
from services.brand import BrandFacts
from services.ux_format import format_toman, to_persian_digits


class ReferralError(Exception):
    """Business-rule failure for referral operations."""


@dataclass(frozen=True)
class ReferralStats:
    referral_code: str
    invite_link: str
    referred_count: int
    pending_rewards: int
    paid_rewards: int


def build_invite_link(*, bot_username: str, referral_code: str) -> str:
    username = bot_username.lstrip("@").strip()
    if not username:
        raise ReferralError("bot_username_missing")
    return f"https://t.me/{username}?start={referral_code}"


async def get_referral_stats(
    session: AsyncSession,
    *,
    user: User,
    bot_username: str,
) -> ReferralStats:
    referred_count = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.referred_by_id == user.id)
        )
        or 0
    )
    pending_rewards = int(
        await session.scalar(
            select(func.count())
            .select_from(ReferralLog)
            .where(
                ReferralLog.referrer_id == user.id,
                ReferralLog.status == ReferralStatus.pending,
            )
        )
        or 0
    )
    paid_rewards = int(
        await session.scalar(
            select(func.count())
            .select_from(ReferralLog)
            .where(
                ReferralLog.referrer_id == user.id,
                ReferralLog.status == ReferralStatus.paid,
            )
        )
        or 0
    )
    return ReferralStats(
        referral_code=user.referral_code,
        invite_link=build_invite_link(bot_username=bot_username, referral_code=user.referral_code),
        referred_count=referred_count,
        pending_rewards=pending_rewards,
        paid_rewards=paid_rewards,
    )


_INVITE_OPENING = "اگه دنبال VPN پایدار می‌گردی، من از نئو نتورک استفاده می‌کنم."


def build_invite_share_url(*, invite_link: str, referred_discount_percent: int) -> str:
    """Telegram's share sheet, pre-filled with the referrer's own link.

    Telegram puts the link first and the text under it, so the text is the
    pitch's opening and, when there is a discount, its number: the same two
    claims the forwardable pitch makes, and nothing a missing fact could
    falsify. The link is the one the screen shows, or the credit is lost.
    """
    lines = [_INVITE_OPENING]
    if referred_discount_percent > 0:
        percent = to_persian_digits(referred_discount_percent)
        lines.append(f"با همین لینک، خرید اولت {percent}٪ تخفیف داره.")
    text = "\n".join(lines)
    return f"https://t.me/share/url?url={quote(invite_link, safe='')}&text={quote(text, safe='')}"


def build_invite_pitch(
    facts: BrandFacts,
    *,
    invite_link: str,
    referred_discount_percent: int,
) -> str:
    """The message a referrer forwards to a friend, as-is.

    Written to BRAND.md: «تو», short sentences, no superlatives, no invented
    urgency, and the link **last** — a message that opens with a link reads as
    an ad and gets skipped (BRAND.md §6).

    Every claim is dropped when the fact behind it is missing, rather than
    softened into an adjective. No price configured means no price line at all,
    not «ارزون». That rule is the whole point: this text leaves the bot and
    lands in a stranger's chat, where an unprovable number is the most
    expensive thing we could send.
    """
    opening = _INVITE_OPENING

    # Each line is its own provable claim; a deployment with nothing configured
    # yields an empty body and the block collapses rather than leaving a gap.
    body: list[str] = []
    if facts.has_channel:
        body.append("وقتی سروری قطع می‌شه توی کانال می‌گن — همین برام مهم بود.")
    if facts.catalog.cheapest_price_toman is not None:
        price = format_toman(facts.catalog.cheapest_price_toman, persian_digits=True)
        body.append(f"قیمت از {price} تومان شروع می‌شه.")
    if facts.has_support and facts.has_support_hours:
        body.append(f"پشتیبانی فارسی، {facts.support_hours}.")

    lines = [opening]
    if body:
        lines.append("")
        lines.extend(body)

    lines.append("")
    if referred_discount_percent > 0:
        lines.append(
            f"با این لینک، خرید اولت {to_persian_digits(referred_discount_percent)}٪ تخفیف داره:"
        )
    else:
        lines.append("لینک ربات:")
    lines.append(invite_link)
    return "\n".join(lines)


async def get_referral_reward_for_order(session: AsyncSession, order_id: int) -> ReferralLog | None:
    result = await session.execute(select(ReferralLog).where(ReferralLog.order_id == order_id))
    return result.scalar_one_or_none()


async def record_referral_reward_for_order(
    session: AsyncSession,
    *,
    order_id: int,
    reward_type: str = "discount",
    reward_value: int,
) -> tuple[ReferralLog | None, bool]:
    """Create referrer reward when order is ACTIVE. Idempotent per order.

    Returns (log, created). No-op when buyer has no referrer or reward already exists.
    """
    existing = await get_referral_reward_for_order(session, order_id)
    if existing is not None:
        return existing, False

    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise ReferralError("order_not_found")

    if int(order.price_toman or 0) <= 0:
        # A giveaway (trial, gift) moved no money, so it earns the referrer nothing.
        return None, False

    buyer_result = await session.execute(select(User).where(User.id == order.user_id))
    buyer = buyer_result.scalar_one_or_none()
    if buyer is None or buyer.referred_by_id is None:
        return None, False

    if buyer.referred_by_id == buyer.id:
        return None, False

    log = ReferralLog(
        referrer_id=buyer.referred_by_id,
        referred_id=buyer.id,
        order_id=order.id,
        reward_type=reward_type,
        reward_value=int(reward_value),
        status=ReferralStatus.pending,
        paid_at=None,
    )
    session.add(log)
    await session.flush()
    return log, True
