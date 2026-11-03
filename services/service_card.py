"""Shared service-card caption formatting (My Services detail UX)."""

from __future__ import annotations

from datetime import datetime
from html import escape

from bot.texts import texts
from services.accounts import AccountOverview, format_traffic_gb
from services.ux_format import (
    format_jalali,
    has_emoji,
    is_expiring_soon,
    time_bar_label,
    to_persian_digits,
    traffic_bar_label,
)

TELEGRAM_CAPTION_MAX = 1024


def truncate_caption(text: str, *, limit: int = TELEGRAM_CAPTION_MAX) -> str:
    """Keep Telegram photo captions within the hard 1024-char limit."""
    if len(text) <= limit:
        return text
    ellipsis = "…"
    keep = max(0, limit - len(ellipsis))
    return text[:keep].rstrip() + ellipsis


def _fmt_expire(value: datetime | None, *, timezone: str | None = None) -> str:
    return format_jalali(
        value, timezone=timezone, unknown=texts.account_unknown, persian_digits=True
    )


def _status_fa(status: str | None) -> str:
    if not status:
        return texts.account_unknown
    return texts.account_status_labels.get(status, status)


def _gb_fa(nbytes: int) -> str:
    """«۱۰» or «۲٫۵۰»: Persian digits and the Persian decimal mark."""
    return to_persian_digits(format_traffic_gb(nbytes)).replace(".", "٫")


def _fmt_used(used: int | None) -> str:
    if used is None:
        return texts.account_unknown
    return f"{_gb_fa(used)} گیگ"


def _fmt_limit(limit: int | None, *, live: bool) -> str:
    if limit is None:
        return texts.account_unlimited if live else texts.account_unknown
    if limit <= 0:
        return texts.account_unlimited
    return f"{_gb_fa(limit)} گیگ"


def _sub_url_html(url: str | None) -> str:
    if not url:
        return texts.account_no_url
    return f"<code>{escape(url)}</code>"


def format_overview_text(
    overview: AccountOverview,
    *,
    warn_days: int = 7,
    timezone: str | None = None,
    badge_emoji: bool = True,
    show_customer: bool = False,
) -> str:
    """Persian overview body matching My Services detail caption.

    ``badge_emoji=False`` renders the expiry badge as plain text. The caller
    passes it when the message this body joins already carries an emoji, so the
    delivered message stays within BRAND.md's one-emoji rule.

    ``show_customer=True`` adds the shop's own label above the panel username.
    Off by default because a plain customer is looking at **their own**
    service: for them the two names are the same string wearing different
    suffixes, and «مشتری:» is the wrong word entirely. A reseller needs both —
    their note («رضا | 09123456789») to know whose it is, and the panel
    username to find the account in PasarGuard when something breaks there.
    """
    label = overview.panel_username or overview.display_name
    shop_label = (overview.display_name or "").strip() if show_customer else ""
    if shop_label == (overview.panel_username or "").strip():
        shop_label = ""
    live = overview.is_live
    # Unlimited traffic: live None/<=0 → نامحدود; stale unknown limit stays unknown (no fake bar).
    if overview.data_limit is None and not live:
        traffic_bar = texts.account_unknown
    else:
        traffic_bar = traffic_bar_label(
            overview.used_traffic,
            overview.data_limit,
            unlimited_label=texts.account_unlimited,
            persian_digits=True,
        )
    time_bar = time_bar_label(
        overview.expire,
        total_days=overview.duration_days,
        unlimited_label="بدون انقضا",
        persian_digits=True,
    )
    badge = ""
    if is_expiring_soon(overview.expire, warn_days=warn_days):
        badge = texts.account_expire_badge if badge_emoji else texts.account_expire_badge_plain
    body = texts.account_overview.format(
        title=(
            texts.account_overview_title_customer
            if show_customer
            else texts.account_overview_title_own
        ),
        expire_badge=badge,
        status=_status_fa(overview.status),
        expire=_fmt_expire(overview.expire, timezone=timezone),
        time_bar=time_bar,
        used=_fmt_used(overview.used_traffic),
        limit=_fmt_limit(overview.data_limit, live=live),
        traffic_bar=traffic_bar,
        sub_url=_sub_url_html(overview.subscription_url),
    )
    head = ""
    if shop_label:
        head += f"👤 مشتری: {escape(shop_label)}\n"
    if label:
        head += f"📡 سرویس: <code>{escape(label)}</code>\n"
    if head:
        body = f"{head}\n{body}"
    if overview.stale:
        return f"{texts.account_stale}\n\n{body}"
    return body


def build_service_card_caption(
    overview: AccountOverview,
    *,
    warn_days: int = 7,
    timezone: str | None = None,
    intro: str | None = None,
) -> str:
    """Caption = optional intro + overview body, truncated for photo captions.

    Three intros carried their own emoji (order ready, trial ready, renewal
    reminder) and the expiry badge added a second one — the renewal reminder
    even repeated ⚠️ twice in one message. The badge yields when the intro has
    already spent the message's single emoji.
    """
    intro_clean = (intro or "").strip()
    body = format_overview_text(
        overview,
        warn_days=warn_days,
        timezone=timezone,
        badge_emoji=not has_emoji(intro_clean),
    )
    text = f"{intro_clean}\n\n{body}" if intro_clean else body
    return truncate_caption(text)
