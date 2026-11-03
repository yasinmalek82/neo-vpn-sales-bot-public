"""The admin home screen, rendered in one place.

It is opened from /admin, the reply-keyboard «پنل مدیریت» button and every
«🏠» button. Three copies of the text and keyboard drifted once; one function
keeps the status block the same wherever the owner lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.admin import admin_home_keyboard
from bot.texts import texts
from services.admin_home import HomeStatus, get_home_status
from services.ux_format import format_relative_age, format_toman, to_persian_digits


def home_text(status: HomeStatus) -> str:
    lines = [texts.admin_home, ""]
    if status.pending:
        line = texts.admin_home_pending.format(n=to_persian_digits(status.pending))
        if status.stale:
            line += texts.admin_home_pending_stale.format(n=to_persian_digits(status.stale))
        lines.append(line)
    else:
        lines.append(texts.admin_home_pending_none)
    lines.append(
        texts.admin_home_today.format(
            n=to_persian_digits(status.today.orders),
            amount=format_toman(status.today.revenue_toman, persian_digits=True),
        )
    )
    now = datetime.now(UTC)
    # The panel first: while it is unreachable every node line below is stale anyway.
    warnings = [
        (texts.admin_home_warn_panel if d.is_panel else texts.admin_home_warn_node).format(
            name=escape(d.name),
            since=format_relative_age(d.since, now=now, persian_digits=True),
        )
        for d in sorted(status.panel_down, key=lambda d: not d.is_panel)
    ]
    warnings += [
        texts.admin_home_warn_group.format(title=escape(t)) for t in status.groups_without_panel
    ]
    if not status.report_attached:
        warnings.append(texts.admin_home_warn_report_off)
    elif status.report_unready:
        warnings.append(
            texts.admin_home_warn_report_unready.format(n=to_persian_digits(status.report_unready))
        )
    if warnings:
        lines.append("")
        lines.extend(warnings)
    return "\n".join(lines)


async def admin_home_screen(
    session: AsyncSession, web_panel_url: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """``web_panel_url`` is ``settings.web_panel_link``: empty while the panel is off."""
    status = await get_home_status(session)
    return home_text(status), admin_home_keyboard(
        pending_count=status.pending, web_panel_url=web_panel_url
    )
