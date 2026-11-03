"""Referral invite UX + free trial claim."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.delivery import deliver_provisioned_service_card
from bot.handlers.sale_gate import refuse_if_panel_down
from bot.keyboards.main_menu import MenuCallback, build_main_menu_keyboard
from bot.keyboards.referral import (
    ReferralCB,
    invite_share_keyboard,
    referral_keyboard,
    reseller_trial_recovery_keyboard,
    trial_exit_keyboard,
    trial_groups_keyboard,
    trial_services_keyboard,
)
from bot.texts import texts
from database.models import Order, User
from services.accounts import plain_title
from services.brand import get_brand_facts, resolve_bot_username, with_support
from services.customer_label import CustomerLabel
from services.finance import settle_reseller_trial_provision_failure
from services.panel_settings import get_trial_hwid_limit
from services.referral import (
    ReferralError,
    build_invite_pitch,
    build_invite_share_url,
    get_referral_stats,
)
from services.store_config import category_title_by_slug
from services.trial import (
    TrialError,
    TrialOffer,
    TrialResult,
    get_reseller_trial_quota,
    issue_free_trial,
    issue_reseller_trial,
    list_trial_offers,
    resume_reseller_trial,
    user_trial_eligible,
)
from services.ux_format import format_toman, to_persian_digits

logger = logging.getLogger(__name__)
router = Router(name="referral")


def _tg_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if isinstance(message, Message):
        return message
    return None


def _reseller_resume_order_id(data: str | None) -> int | None:
    """Parse only a positive order id from the explicit recovery callback."""
    if not data or not data.startswith(ReferralCB.RESELLER_TRIAL_RESUME_PREFIX):
        return None
    suffix = data.removeprefix(ReferralCB.RESELLER_TRIAL_RESUME_PREFIX)
    if not suffix.isdigit():
        return None
    order_id = int(suffix)
    return order_id if order_id > 0 else None


@router.callback_query(F.data == MenuCallback.REFERRAL)
async def menu_referral(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await render_referral(message, session, settings, bot, db_user)


async def render_referral(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    db_user: User | None,
) -> None:
    """The invite screen, reachable from the inline menu or a Message entry."""
    if db_user is None:
        await message.answer(texts.account_need_user)
        return
    try:
        username = await resolve_bot_username(bot, settings)
        stats = await get_referral_stats(session, user=db_user, bot_username=username)
    # BRAND.md rule 6: a failure names a human. «دوباره امتحان کن» alone leaves
    # someone whose invite screen will not load with nowhere left to go.
    except ReferralError:
        logger.exception("referral stats failed")
        await message.answer(with_support(texts.error_generic, settings))
        return
    except Exception:
        logger.exception("referral UX failed")
        await message.answer(with_support(texts.error_generic, settings))
        return

    eligible = False
    if settings.trial_enabled:
        eligible = await user_trial_eligible(session, settings, db_user)

    text = texts.referral_home.format(
        code=stats.referral_code,
        referred=to_persian_digits(stats.referred_count),
        pending=to_persian_digits(stats.pending_rewards),
        paid=to_persian_digits(stats.paid_rewards),
        referred_discount=to_persian_digits(settings.referral_referred_discount_percent),
    )
    await message.answer(text, reply_markup=referral_keyboard(trial_eligible=eligible))

    # The pitch goes out as its **own** message so it can be forwarded whole.
    # Folding it into the dashboard above would forward the referrer's reward
    # counters to their friend along with it.
    facts = await get_brand_facts(session, settings)
    pitch = build_invite_pitch(
        facts,
        invite_link=stats.invite_link,
        referred_discount_percent=settings.referral_referred_discount_percent,
    )
    share_url = build_invite_share_url(
        invite_link=stats.invite_link,
        referred_discount_percent=settings.referral_referred_discount_percent,
    )
    await message.answer(
        f"{texts.referral_pitch_intro}\n\n{pitch}",
        reply_markup=invite_share_keyboard(share_url),
    )


@router.callback_query(F.data == ReferralCB.BACK)
async def referral_back(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    is_admin = bool(db_user and db_user.is_admin)
    user_id = db_user.id if db_user else None
    markup = await build_main_menu_keyboard(
        session,
        user_id=user_id,
        is_admin=is_admin,
        is_reseller=bool(getattr(db_user, "is_reseller", False)),
    )
    await message.answer(texts.main_menu, reply_markup=markup)


@router.callback_query(F.data == ReferralCB.TRIAL)
@router.callback_query(F.data == MenuCallback.TRIAL)
async def claim_trial(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    await render_trial(message, session, settings, db_user, state)


@router.callback_query(F.data.startswith(ReferralCB.TRIAL_GROUP_PREFIX))
async def claim_trial_for_group(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = _tg_message(callback)
    if message is None:
        return
    slug = (callback.data or "").removeprefix(ReferralCB.TRIAL_GROUP_PREFIX)
    await render_trial(message, session, settings, db_user, state, slug=slug)


def _pick_offer(offers: list[TrialOffer], slug: str | None) -> TrialOffer | None:
    if slug is None:
        return offers[0] if len(offers) == 1 else None
    return next((o for o in offers if o.slug == slug), None)


async def _customer_offer(
    message: Message, session: AsyncSession, settings: Settings, slug: str | None
) -> TrialOffer | None:
    """The group whose trial to issue, or ``None`` once the customer was answered."""
    offers = await list_trial_offers(session, settings)
    if not offers:
        await message.answer(texts.trial_disabled, reply_markup=trial_exit_keyboard())
        return None
    offer = _pick_offer(offers, slug)
    if offer is None:
        body = texts.trial_pick_group if slug is None else texts.trial_group_gone
        await message.answer(body, reply_markup=trial_groups_keyboard(offers))
    return offer


async def render_trial(  # noqa: PLR0911 - one early answer per refusal
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    state: FSMContext,
    *,
    slug: str | None = None,
) -> None:
    """Claim a group's trial, or say plainly why it cannot be claimed.

    Reachable from the main menu, the bottom keyboard and the invite screen —
    one path, so the three entries cannot drift apart. With one group offering
    a trial it is claimed straight away; with more, the customer picks, since
    each group's trial tests a different product. Every exit carries a
    keyboard: this screen is a leaf, and a leaf without one is a dead end.
    """
    if db_user is None:
        await message.answer(texts.account_need_user, reply_markup=trial_exit_keyboard())
        return
    if not settings.trial_enabled:
        await message.answer(texts.trial_disabled, reply_markup=trial_exit_keyboard())
        return
    if await refuse_if_panel_down(message, session, settings):
        return

    # A reseller hands trials to their own customers, so the personal
    # «one per person, ever» rule does not describe what they are doing.
    if db_user.is_reseller:
        # Ask who it is for first. A shop with thirty services cannot tell them
        # apart by panel username, and a label added «later» never is.
        await _ask_reseller_trial_label(message, session, settings, db_user, state, slug=slug)
        return

    offer = await _customer_offer(message, session, settings, slug)
    if offer is None:
        return

    hwid_limit = await get_trial_hwid_limit(session, settings)
    client = await get_pasarguard_client(session, settings)
    try:
        result = await issue_free_trial(
            session,
            client,
            user_id=db_user.id,
            category_slug=offer.slug,
            counts_legacy=offer.counts_legacy,
            data_limit_gb=offer.data_limit_gb,
            duration_days=offer.duration_days,
            hwid_limit=hwid_limit,
            group_ids=offer.group_ids,
            enabled=settings.trial_enabled,
        )
        await session.commit()
    except TrialError as exc:
        await session.rollback()
        code = str(exc)
        if code in {"trial_already_claimed", "trial_not_eligible"}:
            # The button is shown to everyone now, so this is a normal answer,
            # not an error: the customer used their one trial already.
            await message.answer(texts.trial_already_claimed, reply_markup=trial_exit_keyboard())
        elif code == "trial_panel_missing":
            await message.answer(texts.trial_panel_missing, reply_markup=trial_exit_keyboard())
        elif code == "trial_disabled":
            await message.answer(texts.trial_disabled, reply_markup=trial_exit_keyboard())
        else:
            logger.warning("trial failed: %s", exc)
            await message.answer(
                with_support(texts.trial_error, settings), reply_markup=trial_exit_keyboard()
            )
        return
    except Exception:
        logger.exception("trial unexpected failure")
        await session.rollback()
        await message.answer(
            with_support(texts.trial_error, settings), reply_markup=trial_exit_keyboard()
        )
        return
    finally:
        await client.aclose()

    if result.already_claimed:
        await message.answer(texts.trial_see_my_services, reply_markup=trial_services_keyboard())
        return

    delivered_days = (
        result.delivery_duration_days
        if result.delivery_duration_days is not None
        else getattr(result.order, "delivery_duration_days", None)
    )
    delivered_gb = (
        result.delivery_data_limit_gb
        if result.delivery_data_limit_gb is not None
        else getattr(result.order, "delivery_data_limit_gb", None)
    )
    shown_days = offer.duration_days if delivered_days is None else delivered_days
    shown_gb = offer.data_limit_gb if delivered_gb is None else delivered_gb
    intro = texts.trial_ready_intro.format(
        group=plain_title(offer.title_fa),
        days=to_persian_digits(shown_days),
        gb=to_persian_digits(shown_gb),
    )
    try:
        await deliver_provisioned_service_card(
            session,
            settings,
            user_id=db_user.id,
            account_id=result.account.id,
            order_id=result.order.id,
            message=message,
            intro=intro,
        )
    except Exception:
        logger.exception("Failed to deliver trial service card for user %s", db_user.id)
        sub_url = result.account.subscription_url or "—"
        await message.answer(
            texts.trial_success.format(
                days=to_persian_digits(shown_days),
                gb=to_persian_digits(shown_gb),
                sub_url=sub_url,
            )
        )


async def _ask_reseller_trial_label(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    state: FSMContext,
    *,
    slug: str | None,
) -> None:
    """Which product, then who it is for — asked before anything is created."""
    offers = await list_trial_offers(session, settings, reseller=True)
    if not offers:
        await message.answer(texts.reseller_trial_none, reply_markup=trial_exit_keyboard())
        return
    offer = _pick_offer(offers, slug)
    if offer is None:
        body = texts.reseller_trial_pick_group if slug is None else texts.trial_group_gone
        await message.answer(body, reply_markup=trial_groups_keyboard(offers))
        return
    quota = await get_reseller_trial_quota(session, db_user.id)
    if not quota.can_issue:
        await message.answer(
            texts.reseller_trial_exhausted.format(
                quota=to_persian_digits(quota.quota), period=to_persian_digits(quota.period_label)
            ),
            reply_markup=trial_exit_keyboard(),
        )
        return
    note = (
        texts.reseller_trial_label_free.format(remaining=to_persian_digits(quota.remaining))
        if quota.is_free
        else texts.reseller_trial_label_paid.format(
            price=format_toman(quota.price_toman, persian_digits=True)
        )
    )
    from bot.handlers.customer_label import FLOW_TRIAL, ask_customer_name

    await ask_customer_name(message, state, flow=FLOW_TRIAL, note=note, trial_slug=offer.slug)


async def _render_reseller_trial(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    *,
    slug: str | None,
    label: str | None = None,
    service_name: str | None = None,
) -> None:
    """Give a reseller one trial service to hand to a prospective customer.

    Free while their monthly quota lasts, then priced from their rate card.
    The quota is always stated, because «چرا این بار پول کم شد؟» is the first
    question a silent charge produces.
    """
    # The label may arrive after the outage began: the check sits where it is issued.
    if await refuse_if_panel_down(message, session, settings):
        return
    quota = await get_reseller_trial_quota(session, db_user.id)
    if not quota.can_issue:
        await message.answer(
            texts.reseller_trial_exhausted.format(
                quota=to_persian_digits(quota.quota), period=to_persian_digits(quota.period_label)
            ),
            reply_markup=trial_exit_keyboard(),
        )
        return

    # Resolved again at issue time: the group may have lost its reseller
    # trial between the pick and the label, and a stale offer would give away
    # a product the owner just switched off.
    offer = _pick_offer(await list_trial_offers(session, settings, reseller=True), slug)
    if offer is None:
        await message.answer(texts.trial_group_gone, reply_markup=trial_exit_keyboard())
        return
    hwid_limit = await get_trial_hwid_limit(session, settings)
    client = await get_pasarguard_client(session, settings)
    try:
        result = await issue_reseller_trial(
            session,
            client,
            user_id=db_user.id,
            data_limit_gb=offer.data_limit_gb,
            duration_days=offer.duration_days,
            hwid_limit=hwid_limit,
            group_ids=offer.group_ids,
            display_name=label,
            service_name=service_name,
            category_slug=offer.slug,
            enabled=settings.trial_enabled,
        )
        await session.commit()
    except TrialError as exc:
        recovery_order = await _commit_reseller_trial_failure(session, settings, exc)
        code = str(exc)
        if code == "insufficient_funds":
            body = texts.reseller_trial_no_funds.format(
                price=format_toman(quota.price_toman, persian_digits=True)
            )
        elif code == "trial_quota_exhausted":
            body = texts.reseller_trial_exhausted.format(
                quota=to_persian_digits(quota.quota), period=to_persian_digits(quota.period_label)
            )
        else:
            logger.warning("reseller trial failed: %s", exc)
            body = with_support(texts.trial_error, settings)
        markup = _reseller_recovery_markup(exc, recovery_order)
        await message.answer(body, reply_markup=markup)
        return
    except Exception:
        logger.exception("reseller trial unexpected failure")
        await session.rollback()
        await message.answer(
            with_support(texts.trial_error, settings), reply_markup=trial_exit_keyboard()
        )
        return
    finally:
        await client.aclose()

    await _deliver_reseller_trial_result(message, session, settings, db_user, result)


async def _commit_reseller_trial_failure(
    session: AsyncSession, settings: Settings, exc: TrialError
) -> Order | None:
    """Persist terminal settlement or unconfirmed recovery state before UX."""
    if exc.order_id is None:
        await session.rollback()
        return None
    try:
        await settle_reseller_trial_provision_failure(
            session,
            order_id=exc.order_id,
            original_exception=exc.original_exception,
            max_n=settings.wallet_provision_fail_credit_max_n,
        )
        await session.commit()
    except Exception:
        logger.exception("reseller trial settlement failed order=%s", exc.order_id)
        await session.rollback()
        return None
    return await session.get(Order, exc.order_id)


def _reseller_recovery_markup(exc: TrialError, order: Order | None) -> InlineKeyboardMarkup:
    """Only panel-recoverable outcomes keep an explicit retry action."""
    if order is None or (exc.original_exception is None and str(exc) != "insufficient_funds"):
        return trial_exit_keyboard()
    return reseller_trial_recovery_keyboard(
        order_id=int(order.id), customer_label=order.customer_label
    )


async def _deliver_reseller_trial_result(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    result: TrialResult,
) -> None:
    """Send success copy from the immutable giveaway snapshot, not live settings."""
    order = result.order
    account = result.account
    after = await get_reseller_trial_quota(session, db_user.id)
    charged = int(result.order.price_toman or 0)
    days = int(order.delivery_duration_days)
    gb = int(order.delivery_data_limit_gb)
    slug = getattr(order, "trial_category_slug", None)  # None on a pre-group giveaway
    group = plain_title(await category_title_by_slug(session, slug)) or "—"
    intro = (
        texts.reseller_trial_free.format(
            group=group,
            days=to_persian_digits(days),
            gb=to_persian_digits(gb),
            remaining=to_persian_digits(after.remaining),
        )
        if charged == 0
        else texts.reseller_trial_charged.format(
            group=group,
            days=to_persian_digits(days),
            gb=to_persian_digits(gb),
            price=format_toman(charged, persian_digits=True),
        )
    )
    try:
        await deliver_provisioned_service_card(
            session,
            settings,
            user_id=db_user.id,
            account_id=account.id,
            order_id=order.id,
            message=message,
            intro=intro,
        )
    except Exception:
        logger.exception("Failed to deliver reseller trial card for %s", db_user.id)
        sub_url = account.subscription_url or "—"
        await message.answer(f"{intro}\n\n{sub_url}", reply_markup=trial_exit_keyboard())


@router.callback_query(F.data.startswith(ReferralCB.RESELLER_TRIAL_RESUME_PREFIX))
async def resume_reseller_trial_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    """Recover exactly the order encoded in the retry action, after DB checks."""
    await callback.answer()
    message = _tg_message(callback)
    order_id = _reseller_resume_order_id(callback.data)
    if message is None or db_user is None or order_id is None:
        return
    client = await get_pasarguard_client(session, settings)
    try:
        result = await resume_reseller_trial(session, client, user_id=db_user.id, order_id=order_id)
        await session.commit()
    except TrialError as exc:
        recovery_order = await _commit_reseller_trial_failure(session, settings, exc)
        markup = _reseller_recovery_markup(exc, recovery_order)
        await message.answer(with_support(texts.trial_error, settings), reply_markup=markup)
        return
    except Exception:
        logger.exception("reseller trial resume unexpected failure order=%s", order_id)
        await session.rollback()
        await message.answer(
            with_support(texts.trial_error, settings), reply_markup=trial_exit_keyboard()
        )
        return
    finally:
        await client.aclose()
    await _deliver_reseller_trial_result(message, session, settings, db_user, result)


async def continue_trial_with_customer_label(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
    *,
    customer: CustomerLabel,
) -> None:
    """Issue the trial once `bot.handlers.customer_label` has name and phone."""
    slug = (await state.get_data()).get("trial_slug")
    await state.clear()
    if db_user is None or not db_user.is_reseller:
        return
    await _render_reseller_trial(
        message,
        session,
        settings,
        db_user,
        slug=str(slug) if slug else None,
        label=str(customer),
        service_name=customer.name,
    )
