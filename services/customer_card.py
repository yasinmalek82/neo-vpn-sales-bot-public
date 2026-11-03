"""The customer card's facts, composed once for the bot and the web panel.

The Telegram card (``bot/handlers/admin.py::_card_text``) and the panel's card
read the same strings from here, so a role, a trial line or a money figure
cannot differ between the two. ``esc`` is the caller's escaper for user input
(name, username, group title, referral code): the bot passes ``html.escape``
because it sends HTML; the panel passes nothing because Jinja escapes on output.

Moved here from the handler unchanged: ``customer_name``, ``role_label``,
``trial_line``, the field formatting of ``_card_text`` and the ledger rows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bot.texts import texts
from database.models import User
from database.models.wallet import WalletEntryType
from services.customer_lookup import CustomerCard
from services.ux_format import (
    fa_toman,
    format_jalali,
    format_relative_age,
    to_persian_digits,
)
from services.wallet import wallet_reason_label

__all__ = ["CardFacts", "LedgerLine", "card_facts", "customer_name", "role_label"]


def _same(text: str) -> str:
    return text


def customer_name(user: User) -> str:
    return user.first_name or user.username or str(user.telegram_id)


def role_label(user: User) -> str:
    """Admin wins over reseller: someone who is both reads as admin, because
    that is the permission that actually governs what they can do."""
    if user.is_admin:
        return texts.admin_customer_role_admin
    if user.is_reseller:
        return texts.admin_customer_role_reseller
    return texts.admin_customer_role_customer


@dataclass(frozen=True)
class LedgerLine:
    """One recent wallet entry: ``sign`` is «+» or «−», ``amount`` in Persian digits."""

    sign: str
    amount: str
    label: str


@dataclass(frozen=True)
class CardFacts:
    """Every field of the card as display text. All user input is already ``esc``-aped."""

    name: str
    handle: str
    telegram_id: int
    status: str
    role: str
    is_banned: bool
    is_admin: bool
    is_reseller: bool
    joined: str
    last_seen: str
    trial: str
    wallet: str
    topped_up: str
    spent: str
    orders_total: str
    orders_paid: str
    last_order: str
    referrals: str
    referrer: str
    referral_code: str
    services_count: str
    ledger: list[LedgerLine]


def trial_line(
    card: CustomerCard, esc: Callable[[str], str] = _same, *, none: str | None = None
) -> str:
    """One trial per store group: «ویژه ✓ ۱۴۰۵/۰۷/۰۸ • نامحدود —»."""
    parts = [
        (
            texts.admin_customer_trial_taken.format(
                title=esc(mark.title),
                when=format_jalali(mark.taken_at, persian_digits=True).split(" ")[0],
            )
            if mark.taken_at is not None
            else (
                texts.admin_customer_trial_legacy_taken.format(title=esc(mark.title))
                if mark.legacy_consumed
                else texts.admin_customer_trial_open.format(title=esc(mark.title))
            )
        )
        for mark in card.trials or []
    ]
    return " • ".join(parts) or (texts.admin_customer_trial_none if none is None else none)


def card_facts(
    card: CustomerCard, esc: Callable[[str], str] = _same, *, web: bool = False
) -> CardFacts:
    """``web`` writes an empty value as «ندارد»; the bot's card keeps its «—»."""
    user = card.user
    empty = texts.web_customer_empty if web else "—"
    return CardFacts(
        name=esc(customer_name(user)),
        handle=f"@{esc(user.username)}" if user.username else "بدون یوزرنیم",
        telegram_id=user.telegram_id,
        status=(
            texts.admin_customer_status_banned if user.is_banned else texts.admin_customer_status_ok
        ),
        role=role_label(user),
        is_banned=bool(user.is_banned),
        is_admin=bool(user.is_admin),
        is_reseller=bool(user.is_reseller),
        joined=format_jalali(user.created_at, persian_digits=True),
        last_seen=format_relative_age(user.last_seen_at, unknown=empty, persian_digits=True),
        trial=trial_line(card, esc, none=empty if web else None),
        wallet=fa_toman(card.wallet_balance_toman),
        topped_up=fa_toman(card.topped_up_toman),
        spent=fa_toman(card.spent_toman),
        orders_total=to_persian_digits(card.orders_total),
        orders_paid=to_persian_digits(card.orders_paid),
        last_order=format_jalali(card.last_order_at, unknown=empty, persian_digits=True),
        referrals=to_persian_digits(card.referral_count),
        referrer=esc(card.referrer_label or (empty if web else texts.admin_customer_no_referrer)),
        referral_code=esc(user.referral_code or empty),
        services_count=to_persian_digits(len(card.services)),
        ledger=[
            LedgerLine(
                sign="+" if entry.entry_type is WalletEntryType.credit else "−",
                amount=fa_toman(int(entry.amount_toman)),
                label=esc(wallet_reason_label(entry.reason)),
            )
            for entry in card.recent_ledger
        ],
    )
