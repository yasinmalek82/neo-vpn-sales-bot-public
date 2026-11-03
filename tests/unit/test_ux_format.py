"""Unit tests for progress bars, status emoji, and wallet menu label."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.ux_format import (
    format_jalali,
    format_toman,
    is_expiring_soon,
    progress_bar,
    service_status_emoji,
    time_bar_label,
    to_persian_digits,
    traffic_bar_label,
    wallet_menu_label,
)


def test_progress_bar_basic() -> None:
    assert progress_bar(0.45, width=10) == "▓▓▓▓▓░░░░░ 45%"
    assert progress_bar(0.0, width=10) == "░░░░░░░░░░ 0%"
    assert progress_bar(1.0, width=10) == "▓▓▓▓▓▓▓▓▓▓ 100%"
    assert progress_bar(1.5, width=5) == "▓▓▓▓▓ 100%"
    assert progress_bar(-1, width=5) == "░░░░░ 0%"


def test_traffic_bar_unlimited_no_fake_bar() -> None:
    assert traffic_bar_label(100, None) == "نامحدود"
    assert traffic_bar_label(100, 0) == "نامحدود"
    assert traffic_bar_label(None, -1, unlimited_label="نامحدود") == "نامحدود"
    used = 5 * 1024**3
    limit = 10 * 1024**3
    assert "50%" in traffic_bar_label(used, limit)
    assert "▓" in traffic_bar_label(used, limit)


def test_time_bar_countdown_and_total() -> None:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    expire = now + timedelta(days=45)
    assert time_bar_label(None) == "بدون انقضا"
    label = time_bar_label(expire, total_days=90, now=now)
    assert "50%" in label
    countdown = time_bar_label(expire, total_days=None, now=now)
    assert "45 روز مانده" in countdown
    past = now - timedelta(days=1)
    assert "منقضی" in time_bar_label(past, now=now) or "0%" in time_bar_label(
        past, total_days=30, now=now
    )


def test_service_status_emoji() -> None:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    assert service_status_emoji("active", now + timedelta(days=30), warn_days=7, now=now) == "🟢"
    assert service_status_emoji("active", now + timedelta(days=3), warn_days=7, now=now) == "🟡"
    assert service_status_emoji("active", now - timedelta(days=1), warn_days=7, now=now) == "🔴"
    assert service_status_emoji("disabled", now + timedelta(days=30), warn_days=7, now=now) == "🔴"
    assert service_status_emoji("expired", None, warn_days=7, now=now) == "🔴"
    assert service_status_emoji("PROVISION_FAILED", None, warn_days=7, now=now) == "🔴"


def test_is_expiring_soon() -> None:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    assert is_expiring_soon(now + timedelta(days=7), warn_days=7, now=now)
    assert not is_expiring_soon(now + timedelta(days=8), warn_days=7, now=now)
    assert not is_expiring_soon(None, warn_days=7, now=now)
    assert not is_expiring_soon(now - timedelta(days=1), warn_days=7, now=now)


def test_wallet_menu_label_persian_digits() -> None:
    assert wallet_menu_label(None).endswith("کیف پول من")
    label = wallet_menu_label(123_000)
    assert "کیف پول (" in label
    assert "۱۲۳٬۰۰۰" in label
    assert to_persian_digits("123") == "۱۲۳"
    assert format_toman(150_000, persian_digits=True) == "۱۵۰٬۰۰۰"


def test_format_jalali_tehran() -> None:
    # 2025-09-12 13:00 UTC → 1404/06/21 16:30 Asia/Tehran
    dt = datetime(2025, 9, 12, 13, 0, tzinfo=UTC)
    assert format_jalali(dt) == "1404/06/21 16:30"
    assert format_jalali(dt, timezone="Asia/Tehran") == "1404/06/21 16:30"
    naive = datetime(2025, 9, 12, 13, 0)  # treated as UTC
    assert format_jalali(naive) == "1404/06/21 16:30"
    assert format_jalali(None) == "—"
    assert format_jalali(None, unknown="?") == "?"
    # Invalid tz falls back to Tehran
    assert format_jalali(dt, timezone="Not/AZone") == "1404/06/21 16:30"


def test_a_service_delivered_seconds_ago_reads_a_full_time_bar() -> None:
    """Live: the bar read 97% right after a 30-day purchase (29 whole days / 30)."""
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    delivered = time_bar_label(now + timedelta(days=30, seconds=-20), total_days=30, now=now)
    assert delivered.endswith(" 100%"), delivered
    half = time_bar_label(now + timedelta(days=15), total_days=30, now=now)
    assert half.endswith(" 50%"), half
    last_hours = time_bar_label(now + timedelta(hours=18), total_days=30, now=now)
    assert last_hours.endswith(" 3%"), last_hours  # not 0% while it still works


def test_time_left_rounds_to_the_day_and_says_hours_under_a_day() -> None:
    """Live: a 1-day trial an hour old read «۰ روز», a 60-day renewal «۵۹ روز»."""
    from services.ux_format import time_left_fa

    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    assert time_left_fa(now + timedelta(days=60, minutes=-3), now=now) == "۶۰ روز"
    assert time_left_fa(now + timedelta(days=12, hours=11), now=now) == "۱۲ روز"
    assert time_left_fa(now + timedelta(days=12, hours=13), now=now) == "۱۳ روز"
    assert time_left_fa(now + timedelta(hours=23), now=now) == "۲۳ ساعت"
    assert time_left_fa(now + timedelta(hours=23, minutes=58), now=now) == "۱ روز"
    assert time_left_fa(now + timedelta(minutes=20), now=now) == "کمتر از یک ساعت"


def test_the_admin_services_list_and_card_use_the_same_time_left() -> None:
    from types import SimpleNamespace

    from bot.handlers import admin as ha
    from bot.keyboards.admin import service_row_label

    def snap(**left: float) -> SimpleNamespace:
        moment = datetime.now(UTC) + timedelta(**left, seconds=30)
        return SimpleNamespace(
            status="active", expire_at=moment, is_unlimited_data=True, remaining_bytes=None
        )

    assert service_row_label("trial", snap(hours=22)) == "🟢 trial • ۲۲ ساعت"
    assert service_row_label("ali", snap(days=59, hours=23.9)) == "🟢 ali • ۶۰ روز"
    assert ha._fmt_expire(snap(hours=22)).endswith("(۲۲ ساعت مانده)")  # type: ignore[arg-type]
    assert ha._fmt_expire(snap(days=59, hours=23.9)).endswith("(۶۰ روز مانده)")  # type: ignore[arg-type]
