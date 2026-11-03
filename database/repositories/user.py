"""User persistence helpers for Telegram onboarding."""

from __future__ import annotations

import logging
import secrets
import string

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, User
from services.report_events import user_joined
from services.report_outbox import enqueue_report

logger = logging.getLogger(__name__)


def _generate_referral_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_user_by_referral_code(session: AsyncSession, code: str) -> User | None:
    result = await session.execute(select(User).where(User.referral_code == code))
    return result.scalar_one_or_none()


async def _has_completed_purchase(session: AsyncSession, user_id: int) -> bool:
    """Has this user ever got as far as paying? Rejected and abandoned orders
    do not count — pressing /start once and walking away leaves those behind."""
    result = await session.execute(
        select(Order.id)
        .where(
            Order.user_id == user_id,
            Order.status.in_(
                (
                    OrderStatus.PAYMENT_APPROVED,
                    OrderStatus.PROVISIONING,
                    OrderStatus.ACTIVE,
                    OrderStatus.PROVISION_FAILED,
                )
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _attribute_existing_user(
    session: AsyncSession,
    user: User,
    start_payload: str | None,
) -> str | None:
    """Decide what an invite link means for someone already in the database.

    This path used to return ``None`` without even reading the payload, so an
    invite opened by anyone who had ever pressed /start did nothing **and said
    nothing** — indistinguishable from a broken link. It looked broken because
    it was silent, not because refusing was wrong.

    Refusing is right for an *established* customer: otherwise whoever sends
    them a link later harvests a customer they did not bring. But someone who
    only ever pressed /start is not established, and attributing them is what
    the referrer expects.

    Returns a note the caller turns into a Persian sentence — never silence.
    """
    code = (start_payload or "").strip()
    if not code:
        return None

    referrer = await get_user_by_referral_code(session, code)
    if referrer is None:
        note = "invalid"
    elif referrer.telegram_id == user.telegram_id:
        note = "self"
    elif user.referred_by_id is not None:
        note = "already"
    elif await _has_completed_purchase(session, user.id):
        note = "established"
    else:
        user.referred_by_id = referrer.id
        note = "saved"
    return note


async def get_or_create_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    language_code: str | None,
    admin_telegram_ids: list[int],
    start_payload: str | None = None,
) -> tuple[User, bool, str | None]:
    """Return (user, created, referral_note).

    referral_note is a short machine tag: saved | self | invalid | None
    """
    existing = await get_user_by_telegram_id(session, telegram_id)
    if existing is not None:
        existing.username = username
        existing.first_name = first_name
        existing.last_name = last_name
        if language_code:
            existing.language_code = language_code
        existing.is_admin = telegram_id in admin_telegram_ids
        note = await _attribute_existing_user(session, existing, start_payload)
        await session.commit()
        await session.refresh(existing)
        return existing, False, note

    referred_by_id: int | None = None
    referral_note: str | None = None
    if start_payload:
        referrer = await get_user_by_referral_code(session, start_payload.strip())
        if referrer is None:
            referral_note = "invalid"
        elif referrer.telegram_id == telegram_id:
            referral_note = "self"
        else:
            referred_by_id = referrer.id
            referral_note = "saved"

    # Ensure unique referral code
    code = _generate_referral_code()
    while await get_user_by_referral_code(session, code) is not None:
        code = _generate_referral_code()

    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        language_code=(language_code or "fa")[:8],
        referral_code=code,
        referred_by_id=referred_by_id,
        is_admin=telegram_id in admin_telegram_ids,
        is_banned=False,
    )
    session.add(user)
    await session.flush()
    await _report_new_user(session, user)
    await session.commit()
    await session.refresh(user)
    return user, True, referral_note


async def _report_new_user(session: AsyncSession, user: User) -> None:
    """The customers topic hears about a first /start. A report failure must not."""
    try:
        report = user_joined(
            telegram_id=int(user.telegram_id),
            name=user.first_name,
            username=user.username,
            link_user_id=int(user.id),
        )
        await enqueue_report(
            session,
            event_key=report.event_key,
            topic_key=report.topic_key,
            body=report.body,
            link_user_id=report.link_user_id,
        )
    except Exception:
        logger.exception("Could not queue the new-user report")
