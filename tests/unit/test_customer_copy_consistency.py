"""The copy pass of 2026-10-03, pinned so it does not rot.

The owner asked for one voice in everything a customer reads: Persian digits,
Persian punctuation, and an emoji that names its line. These tests scan the
real strings (``customer_strings()``) and the real formatters; none restates
the wording it checks.
"""

from __future__ import annotations

import re

from bot.handlers import account, payment, purchase, wallet
from services.ux_format import fa_toman

_LATIN_DIGIT = re.compile(r"[0-9]")


def has_latin_digit(text: str) -> bool:
    return bool(_LATIN_DIGIT.search(text))


# --- (c) money and the order summary ----------------------------------------


def test_the_customer_money_formatter_gives_persian_digits_and_the_thousand_mark() -> None:
    assert fa_toman(70000) == "۷۰٬۰۰۰"
    assert fa_toman(0) == "۰"
    assert fa_toman(1234567) == "۱٬۲۳۴٬۵۶۷"


def test_every_customer_screen_formats_money_through_the_one_function() -> None:
    """Four modules each kept a private copy that printed Latin digits."""
    for module in (purchase, account, wallet, payment):
        assert module._fmt_toman(150000) == "۱۵۰٬۰۰۰", module.__name__
        assert module._fmt_toman is fa_toman, module.__name__


def _outside_code(text: str) -> str:
    """The prose of a message: what a customer reads, not what they copy."""
    return re.sub(r"<code>.*?</code>", "", text, flags=re.S)


def test_the_service_card_has_no_latin_digit_outside_code() -> None:
    from datetime import UTC, datetime, timedelta

    from services.accounts import AccountOverview
    from services.service_card import build_service_card_caption

    overview = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime.now(UTC) + timedelta(days=12),
        used_traffic=int(2.5 * 1024**3),
        data_limit=10 * 1024**3,
        duration_days=30,
        subscription_url="https://panel.example.com/sub/neo_1",
        panel_username="neo_u1",
    )
    caption = build_service_card_caption(overview, warn_days=30)
    prose = _outside_code(caption)
    assert not has_latin_digit(prose), prose
    assert "۲٫۵۰" in prose and "٪" in prose


# --- the strings themselves --------------------------------------------------
#
# Everything below scans `customer_strings()` (every non-`admin_*` text) and the
# customer messages the code composes, so a new string is covered the day it is
# added.

from tests.unit.test_brand_voice import customer_strings  # noqa: E402

# The connection guides are placeholder copy the owner will rewrite with
# pictures, and the outage announcements are broadcast bodies. Neither is part
# of the pass, so the scans skip them by name.
_OUT_OF_THE_PASS = frozenset(
    {
        "account_howto_pick_platform",
        "account_howto_android",
        "account_howto_ios",
        "account_howto_windows",
        "account_howto_macos",
        "outage_announcement",
        "outage_alternative_line",
        "outage_followup_promise",
        "restore_announcement",
        # Read by the admins only, though the names do not say so.
        "payment_admin_caption",
        "payment_provision_failed_admin_detail",
    }
)

_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_CODE = re.compile(r"<code>.*?</code>", re.S)
_URL = re.compile(r"https?://\S+")
_HANDLE = re.compile(r"@\w+")
# A token with a Latin letter is something people type or copy: `ali12`,
# `ws1_ksdf`, `V2Box`, `REF42`. Digits alone never are.
_LATIN_IDENTIFIER = re.compile(r"(?=\w*[A-Za-z])[A-Za-z0-9_]+")
# Digit-only examples of what to TYPE. None today; naming one here is the way
# to add it, with the string that carries it.
TYPED_EXAMPLES: tuple[str, ...] = ()


def _prose(text: str) -> str:
    """What the customer reads: placeholders, code, links, handles and typed
    identifiers (the allowed Latin-digit contexts) removed."""
    for pattern in (_CODE, _URL, _HANDLE, _PLACEHOLDER, _LATIN_IDENTIFIER):
        text = pattern.sub("", text)
    for example in TYPED_EXAMPLES:
        text = text.replace(example, "")
    return text


def _scanned() -> dict[str, str]:
    return {n: v for n, v in customer_strings().items() if n not in _OUT_OF_THE_PASS}


def test_the_scan_covers_the_copy() -> None:
    assert len(_scanned()) > 100


def test_no_customer_string_has_a_latin_digit_outside_the_allowed_contexts() -> None:
    offenders = {n: _prose(v) for n, v in _scanned().items() if has_latin_digit(_prose(v))}
    assert not offenders, offenders


def test_the_digit_scan_would_catch_a_price_put_back() -> None:
    """Mutation check: a Latin-digit price in a string must be seen."""
    assert has_latin_digit(_prose("مبلغ <b>{amount}</b> تومان — 70,000 تومان"))
    assert not has_latin_digit(_prose("مثال: ali12 و <code>09123</code> و {amount}"))


_PERSIAN_LETTER = r"؀-ۿ"
_BAD_PUNCTUATION: dict[str, re.Pattern[str]] = {
    "space before ، ؛ ؟ or :": re.compile(r"[ \t][،؛؟:]"),
    "no space after ، ؛ or ؟": re.compile(rf"[،؛؟](?=[{_PERSIAN_LETTER}A-Za-z0-9])"),
    "three dots instead of …": re.compile(r"\.\.\."),
    "double space": re.compile(r"[^\S\n] {1,}[^\s]|[^\s\n]  "),
    "trailing space": re.compile(r"[ \t]+$", re.M),
    "ASCII ? , ; next to Persian": re.compile(
        rf"[{_PERSIAN_LETTER}][?,;]|[?,;][{_PERSIAN_LETTER}]"
    ),
}


def test_no_customer_string_has_broken_punctuation_or_spacing() -> None:
    offenders: dict[str, list[str]] = {}
    for name, value in _scanned().items():
        text = _URL.sub("U", _CODE.sub("X", value))
        problems = [label for label, rx in _BAD_PUNCTUATION.items() if rx.search(text)]
        if problems:
            offenders[name] = problems
    assert not offenders, offenders


def test_the_punctuation_scan_would_catch_each_mistake() -> None:
    samples = {
        "space before ، ؛ ؟ or :": "وضعیت : فعال",
        "no space after ، ؛ or ؟": "یکی،دوتا",
        "three dots instead of …": "صبر کن...",
        "double space": "یک  دو",
        "trailing space": "یک \nدو",
        "ASCII ? , ; next to Persian": "چطوری?",
    }
    for label, sample in samples.items():
        assert _BAD_PUNCTUATION[label].search(sample), label
    assert not any(rx.search("وضعیت: فعال، و بعد؟ بله…") for rx in _BAD_PUNCTUATION.values())


def test_a_heading_is_followed_by_a_blank_line_before_its_list() -> None:
    """«heading:» then a bulleted or labelled list reads as one block."""
    offenders: dict[str, str] = {}
    for name, value in _scanned().items():
        lines = value.split("\n")
        for first, second in zip(lines, lines[1:], strict=False):
            if first.rstrip().endswith(":") and second.lstrip().startswith(("•", "-")):
                offenders[name] = first
    assert not offenders, offenders


# --- the glossary: one emoji per concept ------------------------------------
#
# A line shaped «<emoji> برچسب: مقدار» must carry the emoji its label is
# assigned here, on every screen. The label is the whole key, so two screens
# cannot give one concept two icons. The wallet is 🏦 and an amount 💰 (owner,
# 2026-10-03); a few labels name the thing rather than the amount (the card
# payment is 💳, the wallet's own balance line is 🏦).
GLOSSARY: dict[str, str] = {
    "حجم": "📦",
    "حجم مصرف‌شده": "📦",
    "سقف حجم": "📦",
    "مصرف حجم": "📊",
    "مدت": "📅",
    "انقضا": "📅",
    "انقضای جدید": "📅",
    "دورهٔ فعلی": "📅",
    "تمدید سرویس": "📅",
    "زمان باقی‌مانده": "⏳",
    "پاداش در انتظار": "⏳",
    "وضعیت": "🚦",
    "لینک سابسکریپشن": "🔗",
    "سرویس": "📡",
    "نام سرویس": "📡",
    "مشتری": "👤",
    "به نام": "👤",
    "نوع": "🗂",
    "گروه": "📂",
    "کاربر همزمان": "👥",
    "دعوت‌شده‌ها": "👥",
    "شماره کارت": "💳",
    "کارت اعلام‌شده": "💳",
    "نام سرویس در پنل": "📡",
    "مبلغ قابل پرداخت کارت": "💳",
    "بانک": "🏛",
    "قیمت اولیه": "💰",
    "مبلغ نهایی سفارش": "💰",
    "موجودی": "💰",
    "موجودی جدید": "💰",
    "موجودی کیف پول": "🏦",
    "کسر از کیف پول": "🏦",
    "تخفیف": "🏷",
    "کد تو": "🎫",
    "پاداش تسویه‌شده": "🎁",
}
_LABEL_LINE = re.compile(r"^(?:(?P<emoji>[^\s\w<{«(\[•-]\S*) )?(?P<label>[^:\n{<]+?):(?: |$)")


def _label_lines(text: str) -> list[tuple[str | None, str, str]]:
    out = []
    for line in text.split("\n"):
        m = _LABEL_LINE.match(line.strip())
        if m and m.group("label") in GLOSSARY:
            out.append((m.group("emoji"), m.group("label"), line))
    return out


def _composed_messages() -> dict[str, str]:
    """The customer messages the code builds around the texts."""
    from datetime import UTC, datetime, timedelta

    from services.accounts import AccountOverview
    from services.payment_settings import PaymentInfo
    from services.service_card import build_service_card_caption

    overview = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime.now(UTC) + timedelta(days=12),
        used_traffic=2 * 1024**3,
        data_limit=10 * 1024**3,
        duration_days=30,
        subscription_url="https://panel.example.com/sub/neo_1",
        panel_username="neo_u1",
        display_name="رضا",
    )
    volume = {
        "plan_type": "volume",
        "volume_gb": 10,
        "duration_days": 30,
        "hwid_limit": 2,
        "service_name": "ali12",
    }
    unlimited = {"plan_type": "unlimited", "concurrent_users": 3, "duration_months": 2}
    card = PaymentInfo(
        card_number="6037991234567890",
        card_holder_name="علی",
        bank_name="ملی",
        instructions="",
        from_env=frozenset(),
        has_override=False,
    )
    return {
        "service card": build_service_card_caption(overview, warn_days=30),
        "reseller card": build_service_card_caption(overview, warn_days=30)
        + "\n"
        + purchase._summary_details(volume, "📅 تمدید سرویس: ali12"),
        "volume summary": purchase._summary_details(volume),
        "unlimited summary": purchase._summary_details(unlimited),
        "card instructions": payment.format_payment_instructions(
            card, order_id=12, amount_toman=70000
        ),
    }


def test_every_glossary_label_line_carries_its_emoji() -> None:
    sources = {**_scanned(), **_composed_messages()}
    wrong = {
        (name, line): f"want {GLOSSARY[label]}, got {emoji}"
        for name, text in sources.items()
        for emoji, label, line in _label_lines(text)
        if emoji != GLOSSARY[label]
    }
    assert not wrong, wrong


def test_the_glossary_scan_finds_the_screens_it_guards() -> None:
    sources = {**_scanned(), **_composed_messages()}
    seen = {label for text in sources.values() for _, label, _ in _label_lines(text)}
    for label in ("حجم", "مدت", "وضعیت", "لینک سابسکریپشن", "شماره کارت", "تخفیف", "کد تو", "نوع"):
        assert label in seen, label


def test_the_glossary_scan_would_catch_a_wrong_or_missing_emoji() -> None:
    assert _label_lines("💳 حجم: ۱۰ گیگ")[0][0] != GLOSSARY["حجم"]
    assert _label_lines("حجم: ۱۰ گیگ")[0][0] is None
    assert _label_lines("📦 حجم: ۱۰ گیگ")[0][0] == GLOSSARY["حجم"]


def test_one_emoji_is_never_used_for_two_glossary_concepts_it_should_not_share() -> None:
    """📦 is volume and nothing else; 🏦 is only ever the wallet."""
    assert {label for label, emoji in GLOSSARY.items() if emoji == "🏦"} == {
        "موجودی کیف پول",
        "کسر از کیف پول",
    }


# --- one term for one thing --------------------------------------------------

RETIRED_TERMS: dict[str, str] = {
    "ولت": "کیف پول",
    "فیش": "رسید",
    "تایید": "تأیید",
    "تازه": "جدید",
    "اپلیکیشن": "برنامه",
    "این‌جا": "اینجا",
    "می‌آد": "میاد",
    "سرویس‌ت": "سرویست",
}


def _uses(term: str, text: str) -> bool:
    """The whole word only: «ولت» must not match «پولت»."""
    return bool(
        re.search(rf"(?<![{_PERSIAN_LETTER}‌]){re.escape(term)}(?![{_PERSIAN_LETTER}])", text)
    )


def test_no_customer_string_uses_a_retired_spelling_or_synonym() -> None:
    offenders = {
        name: [f"{old} -> {new}" for old, new in RETIRED_TERMS.items() if _uses(old, value)]
        for name, value in _scanned().items()
    }
    offenders = {name: found for name, found in offenders.items() if found}
    assert not offenders, offenders


def test_the_retired_term_scan_matches_whole_words_only() -> None:
    assert _uses("ولت", "ولت من")
    assert not _uses("ولت", "پولت کم شد")
    assert _uses("فیش", "فیش رو بفرست")
    assert _uses("تازه", "پلن تازه")
