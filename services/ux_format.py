"""Small UX formatters: progress bars, Jalali dates, status emoji, Persian digits."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import jdatetime

_FILLED = "▓"
_EMPTY = "░"
_PERSIAN_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Panel / order statuses that mean the service is not healthy.
_BAD_STATUSES = frozenset(
    {
        "disabled",
        "expired",
        "limited",
        "on_hold",
        "EXPIRED",
        "REJECTED",
        "PROVISION_FAILED",
        "DISABLED",
        "LIMITED",
        "ON_HOLD",
    }
)
_OK_STATUSES = frozenset(
    {
        "active",
        "ACTIVE",
        "PAYMENT_APPROVED",
        "PROVISIONING",
    }
)


_EMOJI_RANGES = (
    # Enclosed alphanumerics: 🆓 🆕 🆗 🅰. Easy to miss because they sit *below*
    # the pictograph block — 🆓 in a menu label went uncounted by the emoji
    # budget until it did, which is exactly the hole the budget exists to close.
    (0x1F100, 0x1F1FF),
    (0x1F300, 0x1FAFF),  # pictographs, symbols, supplemental
    (0x231A, 0x23FF),  # watch, hourglass, alarm clock: ⌚ ⏰ ⏳ — were uncounted
    (0x2600, 0x27BF),  # misc symbols + dingbats (⚠ ✅ 👇 …)
    (0x2B00, 0x2BFF),  # arrows/stars
)


def has_emoji(text: str) -> bool:
    """True when ``text`` carries a pictograph.

    Used to keep a composed message inside BRAND.md rule 3 (at most one emoji
    per delivered message). U+FE0F only styles the preceding character, so it
    is not counted on its own.
    """
    return any(
        ch != "\ufe0f" and any(lo <= ord(ch) <= hi for lo, hi in _EMOJI_RANGES) for ch in text
    )


def to_persian_digits(value: str | int) -> str:
    """Map ASCII digits to Persian digits; leave other characters unchanged."""
    return str(value).translate(_PERSIAN_DIGIT_MAP)


_ASCII_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_REPORT_NUMBER_MAP = (
    _ASCII_DIGIT_MAP
    | str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    | str.maketrans({"٬": ",", "٫": "."})
)
_REPORT_URL = r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]+"
_REPORT_HTML_TAG = r"""<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>"""
_REPORT_TECHNICAL_PARTS = re.compile(rf"({_REPORT_HTML_TAG}|{_REPORT_URL})")
_REPORT_URL_PARTS = re.compile(rf"({_REPORT_URL})")


def report_numbers(text: str, *, preserve_html: bool = True) -> str:
    """ASCII numerals in delivered prose; retain HTML attributes and opaque URLs.

    Replacement characters have the same UTF-16 width, preserving Telegram
    entity offsets. This changes presentation only, never stored values or keys.
    """
    protected = _REPORT_TECHNICAL_PARTS if preserve_html else _REPORT_URL_PARTS
    return "".join(
        part if index % 2 else part.translate(_REPORT_NUMBER_MAP)
        for index, part in enumerate(protected.split(text))
    )


def parse_positive_int(raw: str) -> int | None:
    """Accept «150000», «150,000» and «۱۵۰٬۰۰۰»; reject anything else.

    Returns ``None`` rather than raising, so the raw Python ``ValueError`` text
    («invalid literal for int()») can never reach the Persian UI — that leak was
    a real bug once and this is the one place that should know how to avoid it.
    """
    text = raw.strip().replace(",", "").replace("٬", "").replace("،", "")
    text = text.translate(_ASCII_DIGIT_MAP)
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def parse_non_negative_int(raw: str) -> int | None:
    """Like ``parse_positive_int`` but «۰» is a number too; anything else is ``None``."""
    text = raw.strip().replace(",", "").replace("٬", "").replace("،", "")
    text = text.translate(_ASCII_DIGIT_MAP)
    return int(text) if text.isdigit() else None


DEFAULT_DISPLAY_TIMEZONE = "Asia/Tehran"


def format_jalali(
    dt: datetime | None,
    *,
    timezone: str | None = None,
    unknown: str = "—",
    persian_digits: bool = False,
) -> str:
    """Format ``dt`` as Jalali ``YYYY/MM/DD HH:MM`` in the display timezone.

    Naive datetimes are treated as UTC. Invalid timezone names fall back to
    Asia/Tehran.
    """
    if dt is None:
        return unknown
    tz_name = (timezone or DEFAULT_DISPLAY_TIMEZONE).strip() or DEFAULT_DISPLAY_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(tz)
    jalali = jdatetime.datetime.fromgregorian(datetime=local)
    text = jalali.strftime("%Y/%m/%d %H:%M")
    return to_persian_digits(text) if persian_digits else text


def format_toman(value: int, *, persian_digits: bool = False) -> str:
    """Format toman with Persian thousand separator (٬); optional Persian digits."""
    text = f"{int(value):,}".replace(",", "٬")
    if persian_digits:
        return to_persian_digits(text)
    return text


def fa_toman(value: int) -> str:
    """The one money format a customer reads: «۷۰٬۰۰۰», integer toman, no float.

    Every customer screen went through its own copy of ``f"{value:,}"`` and
    printed Latin digits beside Persian ones («۱۰ گیگ» over «70٬000 تومان»).
    """
    return format_toman(int(value), persian_digits=True)


def progress_bar(ratio: float, *, width: int = 10, persian_digits: bool = False) -> str:
    """ASCII/Unicode bar like ``▓▓▓▓▓░░░░░ 45%`` (ratio clamped to 0..1).

    ``persian_digits`` prints «۴۵٪» for a customer-facing card.
    """
    width = max(width, 1)
    clamped = max(0.0, min(1.0, float(ratio)))
    # Half-up rounding so 45% of width 10 → 5 blocks (avoid banker's round).
    filled = min(width, int(clamped * width + 0.5))
    pct = min(100, int(clamped * 100 + 0.5))
    bar = f"{_FILLED * filled}{_EMPTY * (width - filled)}"
    if persian_digits:
        return f"{bar} {to_persian_digits(pct)}٪"
    return f"{bar} {pct}%"


def traffic_bar_label(
    used: int | None,
    limit: int | None,
    *,
    width: int = 10,
    unlimited_label: str = "نامحدود",
    persian_digits: bool = False,
) -> str:
    """Data used/limit bar; unlimited (None/<=0) → clear Persian label, no fake bar."""
    if limit is None or limit <= 0:
        return unlimited_label
    used_v = 0 if used is None else max(0, int(used))
    return progress_bar(used_v / limit, width=width, persian_digits=persian_digits)


def days_until(expire: datetime | None, *, now: datetime | None = None) -> int | None:
    """Whole days remaining until expire (negative if past). None if no expire."""
    if expire is None:
        return None
    return int(_seconds_until(expire, now=now) // 86400)


def _seconds_until(expire: datetime, *, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (expire - current.astimezone(UTC)).total_seconds()


def time_left_fa(expire: datetime, *, now: datetime | None = None) -> str:
    """How long a live service has left: «۶۰ روز», «۲۳ ساعت», «کمتر از یک ساعت».

    Rounded to the nearest day, never floored: a service renewed to 60 days a
    minute ago read «۵۹ روز», and a one-day trial an hour old read «۰ روز».
    Under a day it counts hours. For a moment still in the future.
    """
    seconds = max(0.0, _seconds_until(expire, now=now))
    if seconds < 3600:  # noqa: PLR2004
        return "کمتر از یک ساعت"
    hours = int(seconds / 3600 + 0.5)
    if hours < 24:  # noqa: PLR2004
        return f"{to_persian_digits(hours)} ساعت"
    return f"{to_persian_digits(int(seconds / 86400 + 0.5))} روز"


def time_bar_label(
    expire: datetime | None,
    *,
    total_days: int | None = None,
    now: datetime | None = None,
    width: int = 10,
    unlimited_label: str = "بدون انقضا",
    persian_digits: bool = False,
) -> str:
    """Days remaining/total bar, or countdown text when total is unknown.

    Unlimited / missing expire → clear Persian label without a fake bar.
    """
    if expire is None:
        return unlimited_label
    remaining = days_until(expire, now=now)
    if remaining is None:
        return unlimited_label
    if remaining < 0:
        if total_days and total_days > 0:
            return progress_bar(0.0, width=width, persian_digits=persian_digits)
        return "منقضی شده"
    if total_days is not None and total_days > 0:
        # Remaining / total (full bar = full period left). On the exact time
        # left, not whole days: 29 days 23:59 of 30 floored to 29 read «97%»
        # on a service delivered a minute ago.
        return progress_bar(
            _seconds_until(expire, now=now) / (total_days * 86400),
            width=width,
            persian_digits=persian_digits,
        )
    # Countdown only — no fake percentage without a known total.
    return f"{_digits(remaining, persian_digits)} روز مانده"


def is_expiring_soon(
    expire: datetime | None,
    *,
    warn_days: int,
    now: datetime | None = None,
) -> bool:
    """True when expire is in the future and within ``warn_days`` (inclusive)."""
    if expire is None or warn_days < 0:
        return False
    remaining = days_until(expire, now=now)
    if remaining is None:
        return False
    return 0 <= remaining <= warn_days


def service_status_emoji(
    status: str | None,
    expire: datetime | None,
    *,
    warn_days: int,
    now: datetime | None = None,
) -> str:
    """🟢 active/ok, 🟡 expiring soon, 🔴 expired/disabled/failed."""
    remaining = days_until(expire, now=now)
    normalized = (status or "").strip()
    if remaining is not None and remaining < 0:
        return "🔴"
    if normalized in _BAD_STATUSES:
        return "🔴"
    if is_expiring_soon(expire, warn_days=warn_days, now=now):
        return "🟡"
    if not normalized or normalized in _OK_STATUSES:
        return "🟢"
    # Unknown non-ok status → treat as problem.
    return "🔴"


def wallet_menu_label(balance: int | None) -> str:
    """Main-menu wallet button text with optional Persian-digit balance."""
    if balance is None:
        return "🏦 کیف پول من"
    return f"🏦 کیف پول ({fa_toman(balance)})"


def as_rtl_lines(text: str) -> str:
    """Prefix each line with RLM so Persian howto stays readable in Telegram."""
    rlm = chr(0x200F)
    return chr(10).join(f"{rlm}{line}" if line else rlm for line in text.splitlines())


STALE_PENDING_SECONDS = 3600  # 1 hour


def _digits(value: int, persian: bool) -> str:
    return to_persian_digits(value) if persian else str(value)


def format_relative_age(
    dt: datetime | None,
    *,
    now: datetime | None = None,
    unknown: str = "—",
    persian_digits: bool = False,
) -> str:
    """Relative age for admin screens, e.g. «۱۲ دقیقه قبل».

    ``persian_digits`` follows the ``format_toman`` convention: off by default
    so existing queues keep their exact wording, on where the surrounding text
    is already in Persian numerals — mixing the two inside one card is the
    sloppiness this flag exists to avoid.
    """
    if dt is None:
        return unknown
    current = now or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    seconds = int((current.astimezone(UTC) - dt.astimezone(UTC)).total_seconds())
    seconds = max(seconds, 0)
    if seconds < 60:
        return "همین الان"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{_digits(minutes, persian_digits)} دقیقه قبل"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{_digits(hours, persian_digits)} ساعت قبل"
    days = seconds // 86400
    return f"{_digits(days, persian_digits)} روز قبل"


def pending_age_seconds(dt: datetime | None, *, now: datetime | None = None) -> int | None:
    """Age in seconds; None if dt missing."""
    if dt is None:
        return None
    current = now or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return max(0, int((current.astimezone(UTC) - dt.astimezone(UTC)).total_seconds()))


def is_stale_pending(
    dt: datetime | None,
    *,
    now: datetime | None = None,
    threshold_seconds: int = STALE_PENDING_SECONDS,
) -> bool:
    """True when pending item is older than threshold (default 1h)."""
    age = pending_age_seconds(dt, now=now)
    if age is None:
        return False
    return age >= threshold_seconds


def strip_html_tags(text: str) -> str:
    """The bot's Telegram-HTML sentence as plain text (``<code>15</code>`` → ``15``).

    The bot's admin lines carry ``<b>``/``<code>``; the Mini App shows the same
    sentence, escaped, so the tags are dropped rather than printed.
    """
    import re

    return re.sub(r"</?[a-zA-Z][^>]*>", "", text)
