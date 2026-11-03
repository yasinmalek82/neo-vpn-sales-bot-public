"""The outage announcement, which BRAND.md rule 6 calls the brand's most
important message.

The operator never writes the sentence — only the facts inside it. That is
deliberate: an announcement composed under pressure is exactly where
«متأسفانه مشکلی پیش آمده» and invented causes come from, and both are banned.

These tests also pin the promise «وقتی برگشت همین‌جا خبر می‌دم», because an
outage notice that is never followed up teaches customers to distrust the next
one.
"""

from __future__ import annotations

import re

import pytest

from bot.keyboards.admin import AdminCB, announcement_home_keyboard
from bot.texts import texts
from services.announcements import (
    MAX_FIELD_CHARS,
    AnnouncementError,
    compose_outage,
    compose_restore,
    is_skip,
    now_hhmm,
)
from services.ux_format import has_emoji


def _count_emoji(text: str) -> int:
    return sum(1 for ch in text if has_emoji(ch))


# --- outage ----------------------------------------------------------------


def test_an_outage_names_the_server_and_the_hour() -> None:
    body = compose_outage(server="فنلاند", since="۱۴:۳۰")
    assert "سرور فنلاند از ساعت ۱۴:۳۰ قطعه" in body


def test_an_outage_always_promises_a_follow_up() -> None:
    body = compose_outage(server="فنلاند", since="۱۴:۳۰")
    assert texts.outage_followup_promise in body


def test_an_alternative_is_offered_when_there_is_one() -> None:
    body = compose_outage(server="فنلاند", since="۱۴:۳۰", alternative="لوکیشن آلمان")
    assert "تا درست شدن، از لوکیشن آلمان استفاده کن." in body


@pytest.mark.parametrize("skip", ["-", "ندارم", "ندارد", "خالی", " - "])
def test_no_alternative_means_the_line_is_dropped_not_left_blank(skip: str) -> None:
    """«تا درست شدن، از  استفاده کن» would be worse than saying nothing."""
    body = compose_outage(server="فنلاند", since="۱۴:۳۰", alternative=skip)
    assert "تا درست شدن" not in body
    assert texts.outage_followup_promise in body


def test_is_skip_recognizes_the_ways_an_operator_says_none() -> None:
    assert is_skip("-") and is_skip("ندارم")
    assert not is_skip("آلمان")


def test_an_outage_carries_no_emoji_at_all() -> None:
    """BRAND.md rule 3 allows one; an outage notice does not need it."""
    assert _count_emoji(compose_outage(server="فنلاند", since="۱۴:۳۰", alternative="آلمان")) == 0


def test_the_announcement_never_apologizes_in_the_banned_register() -> None:
    body = compose_outage(server="فنلاند", since="۱۴:۳۰")
    assert "متأسفانه" not in body
    assert "مشکلی پیش آمده" not in body


@pytest.mark.parametrize("field", ["", "   "])
def test_an_empty_server_or_hour_is_refused(field: str) -> None:
    with pytest.raises(AnnouncementError):
        compose_outage(server=field, since="۱۴:۳۰")
    with pytest.raises(AnnouncementError):
        compose_outage(server="فنلاند", since=field)


def test_a_pasted_paragraph_cannot_smuggle_its_own_tone_in() -> None:
    with pytest.raises(AnnouncementError):
        compose_outage(server="ف" * (MAX_FIELD_CHARS + 1), since="۱۴:۳۰")


# --- restore ---------------------------------------------------------------


def test_a_restore_says_it_is_back_and_apologizes_once() -> None:
    body = compose_restore(server="فنلاند")
    assert "سرور فنلاند برگشت" in body
    assert "شرمنده" in body


def test_a_restore_stays_within_one_emoji() -> None:
    assert _count_emoji(compose_restore(server="فنلاند")) == 1


def test_a_restore_needs_a_server_name() -> None:
    with pytest.raises(AnnouncementError):
        compose_restore(server="  ")


# --- time ------------------------------------------------------------------


def test_the_current_hour_is_persian_digits() -> None:
    value = now_hhmm("Asia/Tehran")
    assert re.fullmatch(r"[۰-۹]{2}:[۰-۹]{2}", value), value


def test_an_unknown_timezone_falls_back_instead_of_raising() -> None:
    """A typo in DISPLAY_TIMEZONE must not block an outage announcement."""
    assert re.fullmatch(r"[۰-۹]{2}:[۰-۹]{2}", now_hhmm("Not/AZone"))


# --- wiring ----------------------------------------------------------------


def test_the_restore_button_sits_next_to_the_outage_button() -> None:
    """Remembering to send it later is exactly what does not happen."""
    data = [b.callback_data for row in announcement_home_keyboard().inline_keyboard for b in row]
    assert AdminCB.ANN_OUTAGE in data
    assert AdminCB.ANN_RESTORE in data


def test_every_announcement_callback_has_a_handler() -> None:
    """An orphan here means a dead button in the middle of an outage."""
    import inspect

    import bot.handlers.admin as mod

    source = inspect.getsource(mod)
    for const in ("ANN", "ANN_OUTAGE", "ANN_RESTORE", "ANN_NOW", "ANN_SKIP"):
        assert f"AdminCB.{const}" in source, const


def test_the_announcement_reuses_the_broadcast_pipeline() -> None:
    """The whole point: no second delivery path to keep exactly-once."""
    import inspect

    from bot.handlers.admin import _ann_to_audience

    source = inspect.getsource(_ann_to_audience)
    assert "broadcast_body" in source
    assert "broadcast_audience_keyboard" in source
    # It must not send anything itself.
    assert "create_broadcast" not in source
    assert "send_message" not in source
