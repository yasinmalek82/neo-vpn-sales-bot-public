"""Outage and restore announcements, composed to the brand's shape.

``BRAND.md`` rule 6 — «وقتی خراب شد، صاف بگو» — calls this the most important
thing the brand says, because the competition goes quiet exactly when a customer
is staring at a dead connection. Rule 5 of the same file is why the operator is
never asked to improvise the wording: an announcement written under pressure is
where «متأسفانه مشکلی پیش آمده» and invented causes come from.

So the operator supplies **facts only** — which server, since when, what to use
meanwhile — and the sentence around them is fixed. That also keeps these two
messages inside the tone guard (`tests/unit/test_brand_voice.py`), because they
live in ``bot/texts`` like every other customer-facing string.

Nothing here sends anything. The composed body goes through the existing
broadcast pipeline, so an announcement is resumable and never double-sends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bot.texts import texts
from services.ux_format import DEFAULT_DISPLAY_TIMEZONE, to_persian_digits

# Long enough for a location name («فنلاند ۲»), short enough that a pasted
# paragraph cannot smuggle its own tone into a fixed template.
MAX_FIELD_CHARS = 40

SKIP = "-"


class AnnouncementError(Exception):
    """A field the operator must fix before the announcement can be composed."""


def clean_field(value: str, *, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise AnnouncementError(f"{label} خالی است.")
    if len(text) > MAX_FIELD_CHARS:
        raise AnnouncementError(f"{label} نباید بیش از {MAX_FIELD_CHARS} کاراکتر باشد.")
    return text


def is_skip(value: str) -> bool:
    """«-» means the operator has nothing to offer for an optional field."""
    return (value or "").strip() in {SKIP, "ندارم", "ندارد", "خالی"}


def now_hhmm(timezone: str | None = None) -> str:
    """Current wall time where the customers are, in Persian digits."""
    tz_name = (timezone or DEFAULT_DISPLAY_TIMEZONE).strip() or DEFAULT_DISPLAY_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
    return to_persian_digits(datetime.now(UTC).astimezone(tz).strftime("%H:%M"))


def compose_outage(*, server: str, since: str, alternative: str = SKIP) -> str:
    """«سرور X از ساعت HH:MM قطعه» + what to do meanwhile + a promise to follow up.

    The promise is not decoration: it is the half customers remember, and the
    reason the restore announcement exists as its own button.
    """
    body = texts.outage_announcement.format(
        server=clean_field(server, label="نام سرور"),
        since=clean_field(since, label="ساعت"),
    )
    if not is_skip(alternative):
        line = texts.outage_alternative_line.format(
            alternative=clean_field(alternative, label="جایگزین")
        )
        body = f"{body}\n{line}"
    return f"{body}\n\n{texts.outage_followup_promise}"


def compose_restore(*, server: str) -> str:
    """The message that makes the outage announcement worth trusting next time."""
    return texts.restore_announcement.format(server=clean_field(server, label="نام سرور"))
