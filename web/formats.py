"""Jinja filters for the personal pages: the bot's own formats, not new ones.

Each filter is a thin call into ``services`` so a date, a size or a status reads
the same here as on the bot's service card. No rule lives here.
"""

from __future__ import annotations

from datetime import datetime

from jinja2 import Environment

from bot.texts import texts
from services.accounts import format_traffic_gb
from services.ux_format import (
    format_jalali,
    time_left_fa,
    to_persian_digits,
    traffic_bar_label,
)

EXPIRED_FA = "منقضی شده"


def jalali(value: datetime | None) -> str:
    return format_jalali(value, unknown=texts.account_unknown, persian_digits=True)


def gb(nbytes: int | None) -> str:
    """«۲٫۵۰»: Persian digits and the Persian decimal mark, as the service card prints it."""
    if nbytes is None:
        return texts.account_unknown
    return to_persian_digits(format_traffic_gb(int(nbytes))).replace(".", "٫")


def limit_gb(limit: int | None) -> str:
    """The volume cap; none or zero is «نامحدود» (the panel's meaning, as on the card)."""
    if limit is None or limit <= 0:
        return texts.account_unlimited
    return f"{gb(limit)} گیگ"


def time_left(expire: datetime | None) -> str:
    from datetime import UTC

    if expire is None:
        return texts.account_unknown
    moment = expire if expire.tzinfo else expire.replace(tzinfo=UTC)
    if moment <= datetime.now(UTC):
        return EXPIRED_FA
    return time_left_fa(moment)


def status_fa(status: str | None) -> str:
    if not status:
        return texts.account_unknown
    return texts.account_status_labels.get(status, texts.account_unknown)


def traffic_bar(used: int | None, limit: int | None) -> str:
    return traffic_bar_label(
        used, limit, unlimited_label=texts.account_unlimited, persian_digits=True
    )


def register(env: Environment) -> None:
    env.filters.update(
        {
            "jalali": jalali,
            "gb": gb,
            "limit_gb": limit_gb,
            "time_left": time_left,
            "status_fa": status_fa,
            "traffic_bar": traffic_bar,
        }
    )
    env.globals["expiring_note"] = texts.account_expire_badge_plain.strip()
