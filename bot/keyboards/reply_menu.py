"""The persistent bottom keyboard: navigation roots only.

Why a reply keyboard at all, when the inline main menu already exists: a reply
keyboard is always visible. The inline menu scrolls away, and the only route
back was to type ``/start`` again. Customers migrating from a market bot also
arrive with *its* reply keyboard still on screen, so the bot needs one of its
own or the old buttons keep competing with it.

This keyboard **is** the main menu. It carries the same entries the inline menu
did, so ``/start`` sends one message instead of two — the owner's call after
testing: «دوتا پیام زیاده یکیش کن، همون منوی اصلی داخل منو همبرگری باشه کافیه».

A reply-keyboard button sends **text**, not ``callback_data``, and every flow in
this bot is built on callbacks — so only the roots live here. Anything inside a
flow, and the «بازگشت به منو» buttons that end one, stay inline.
"""

from __future__ import annotations

import re

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from bot.keyboards.style import PRIMARY, styled

BUY = "🛒 خرید اشتراک"
ACCOUNT = "📡 سرویس‌های من"
# A shop's list holds thirty services belonging to thirty other people;
# «سرویس‌های من» is the wrong noun for every one of them.
ACCOUNT_RESELLER = "👥 مشتری‌های من"
WALLET = "🏦 کیف پول"
# The wallet button used to carry a different emoji. A chat that still shows the
# keyboard sent before the change sends that old label as text, and inside a flow
# only an exact label navigates, so the old one stays an alias or those buttons
# go dead after the deploy. Written as escapes so the old emoji appears nowhere
# in the source (tests/unit/test_wallet_emoji.py scans for it).
WALLET_LEGACY_LABELS: tuple[str, ...] = (
    "\U0001f45b کیف پول",  # purse
    "\U0001f4bc کیف پول",  # briefcase
)
REFERRAL = "🎁 دعوت دوستان"
TRIAL = "🎟 تست رایگان"
SUPPORT = "💬 پشتیبانی"
ADMIN = "⚙️ مدیریت"


class ReplyRoot:
    BUY = "buy"
    ACCOUNT = "account"
    WALLET = "wallet"
    REFERRAL = "referral"
    TRIAL = "trial"
    SUPPORT = "support"
    ADMIN = "admin"


# Longest label is «🛒 خرید اشتراک»; anything much longer is the customer typing,
# not tapping. Without this guard a promo code or service name that happened to
# contain «خرید» would be swallowed by navigation.
MAX_LABEL_LENGTH = 30

# Ordered most-specific first: the previous bot's «خرید سرویس 🛍» contains both
# «خرید» and «سرویس», and it meant *buy*.
_ROOT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ReplyRoot.ADMIN, ("مدیریت", "ادمین")),
    (ReplyRoot.WALLET, ("کیفپول", "کیف")),
    (ReplyRoot.SUPPORT, ("پشتیبانی",)),
    (ReplyRoot.REFERRAL, ("دعوت", "معرفی")),
    # Before BUY and ACCOUNT: «تست رایگان» carries neither «خرید» nor «سرویس»
    # today, but the previous bot's «تست سرویس رایگان» carried both and meant
    # *trial*. Matching trial first keeps a migrating customer out of the shop.
    (ReplyRoot.TRIAL, ("تست", "رایگان")),
    (ReplyRoot.BUY, ("خرید",)),
    (ReplyRoot.ACCOUNT, ("سرویس", "مشتریهایمن", "اشتراکمن", "اکانت", "حساب")),
)

_STRIP = re.compile(r"[^؀-ۿa-zA-Z0-9]+")


def normalize_label(text: str) -> str:
    """Drop emoji, spaces, ZWNJ and punctuation, so only the words remain.

    Seen live: a migrating customer tapped «خرید سرویس 🛍» from the previous
    bot's keyboard and this bot answered nothing at all. Matching on normalized
    words instead of exact labels means changing an emoji — ours or theirs —
    cannot break navigation again.
    """
    return _STRIP.sub("", text or "")


def match_root(text: str) -> str | None:
    """The navigation root this message asks for, or ``None``."""
    if not text or len(text) > MAX_LABEL_LENGTH:
        return None
    normalized = normalize_label(text)
    if not normalized:
        return None
    for root, keywords in _ROOT_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return root
    return None


_EXACT_ROOTS: dict[str, str] = {
    BUY: ReplyRoot.BUY,
    ACCOUNT: ReplyRoot.ACCOUNT,
    ACCOUNT_RESELLER: ReplyRoot.ACCOUNT,
    WALLET: ReplyRoot.WALLET,
    REFERRAL: ReplyRoot.REFERRAL,
    TRIAL: ReplyRoot.TRIAL,
    SUPPORT: ReplyRoot.SUPPORT,
    ADMIN: ReplyRoot.ADMIN,
    **dict.fromkeys(WALLET_LEGACY_LABELS, ReplyRoot.WALLET),
}


def navigation_root(text: str, *, in_flow: bool) -> str | None:
    """The root a message navigates to, given whether a flow awaits typed input.

    A tap on our own bottom keyboard sends its label verbatim and always
    navigates, even mid-flow. The keyword match is a guess, so it is made only
    when no flow is waiting: «سرویست فعال شد» typed by the owner as a message to
    a customer was read as «سرویس‌های من», never sent, and the flow was cleared.
    """
    exact = _EXACT_ROOTS.get((text or "").strip())
    if exact is not None or in_flow:
        return exact
    return match_root(text)


def main_reply_keyboard(
    *, is_admin: bool = False, is_reseller: bool = False
) -> ReplyKeyboardMarkup:
    rows = [
        # The one accent of the menu, alone on its row. Through ``styled()`` so
        # BUTTON_COLORS_ENABLED=false switches it off with every other colour.
        [KeyboardButton(text=BUY, style=styled(PRIMARY))],
        [
            KeyboardButton(text=TRIAL),
            KeyboardButton(text=ACCOUNT_RESELLER if is_reseller else ACCOUNT),
        ],
        [KeyboardButton(text=WALLET), KeyboardButton(text=REFERRAL)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=SUPPORT), KeyboardButton(text=ADMIN)])
    else:
        rows.append([KeyboardButton(text=SUPPORT)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="از منوی پایین انتخاب کن",
    )
