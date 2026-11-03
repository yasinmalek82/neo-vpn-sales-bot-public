"""Promo code administration, shared by the bot's wizard and the panel's form.

The bot's store admin and the Mini App both create, switch and delete codes through
the functions here, so the parsing, the limits, the ``counted_from`` stamp and the
Persian change-log note exist once:

* ``parse_code_and_percent`` / ``parse_promo_number`` / ``rules_from_answers`` are the
  wizard's validation, whatever the surface that collected the answers;
* ``save_promo_code`` / ``toggle_promo_code`` / ``delete_promo_code`` write the draft,
  publish with the note, and commit (``upsert_draft_promo`` stamps ``counted_from``
  when the code is not in the draft, so a code made on the web counts from its creation);
* ``promo_lines`` composes what the list shows for each code (rules and live usage).

Checkout (``services/pricing.py::_apply_promo``) stays the one place that refuses an
inactive code: the toggle here writes only what the bot's toggle wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from services.promo_redemptions import PromoRules, live_usage_by_code
from services.store_admin import StoreAdminError, commit_and_activate, get_catalog_view
from services.store_strategy import (
    MAX_PROMO_DAYS,
    MAX_PROMO_LIMIT,
    check_promo_code_and_percent,
    delete_draft_promo,
    list_promo_codes,
    toggle_draft_promo_active,
    upsert_draft_promo,
)
from services.ux_format import format_jalali, parse_non_negative_int, to_persian_digits

# Read straight back to the owner in «تغییرات فروشگاه» (↩️), so Persian.
NOTE_PROMO_SAVED = "کد تخفیف ثبت شد"
NOTE_PROMO_TOGGLED = "کد تخفیف روشن/خاموش شد"
NOTE_PROMO_DELETED = "کد تخفیف حذف شد"

# What ``delete_draft_promo`` and ``toggle_draft_promo_active`` raise for an unknown code.
TEXT_PROMO_MISSING = "این کد تخفیف در پیش‌نویس نیست."


def parse_code_and_percent(code: str, percent: str) -> tuple[str, float]:
    """The wizard's first step: «کد» and «درصد» as typed.

    A missing half is the invalid-input line, a non-number the percent line, anything the
    store rules refuse (shape of the code, 0..100) its own Persian line. ``StoreAdminError``
    carries the text to show.
    """
    code, percent = code.strip(), percent.strip()
    if not code or not percent:
        raise StoreAdminError(texts.admin_store_invalid_input)
    try:
        value = float(percent.replace("%", ""))
    except ValueError as exc:
        raise StoreAdminError(texts.admin_store_promo_invalid_percent) from exc
    try:
        return check_promo_code_and_percent(code, value)
    except StoreAdminError as exc:
        raise StoreAdminError(str(exc) or texts.admin_store_invalid_input) from exc


def parse_promo_number(raw: str, *, maximum: int) -> int:
    """A typed limit or number of days: a whole number from 0 to ``maximum`` (0 = none)."""
    try:
        value = parse_non_negative_int(raw)
    except ValueError:  # «²» passes isdigit() and then int() refuses it
        value = None
    if value is None:
        raise StoreAdminError(texts.admin_promo_invalid_number)
    if value > maximum:
        raise StoreAdminError(
            texts.admin_promo_number_too_big.format(max=to_persian_digits(maximum))
        )
    return value


def _blank_is_zero(raw: str, *, maximum: int) -> int:
    """A form's empty box is the bot's «رد کردن»: no limit."""
    return parse_promo_number(raw, maximum=maximum) if raw.strip() else 0


def answers_from_form(
    *, first_purchase_only: bool, per_user: str, total: str, days: str
) -> dict[str, Any]:
    """The wizard's answers, from a form that holds all four at once.

    «Only the first purchase» answers «uses per customer» (once), exactly as the bot's
    wizard skips that question after a «بله».
    """
    return {
        "first_purchase_only": first_purchase_only,
        "per_user_limit": (
            1 if first_purchase_only else _blank_is_zero(per_user, maximum=MAX_PROMO_LIMIT)
        ),
        "total_limit": _blank_is_zero(total, maximum=MAX_PROMO_LIMIT),
        "days": _blank_is_zero(days, maximum=MAX_PROMO_DAYS),
    }


def rules_from_answers(answers: Mapping[str, Any], *, now: datetime | None = None) -> PromoRules:
    """The wizard's finished answers as the rules stored on the code."""
    days = int(answers.get("days") or 0)
    return PromoRules(
        first_purchase_only=bool(answers.get("first_purchase_only")),
        per_user_limit=int(answers.get("per_user_limit") or 0),
        total_limit=int(answers.get("total_limit") or 0),
        expires_at=(now or datetime.now(UTC)) + timedelta(days=days) if days > 0 else None,
    )


async def save_promo_code(
    session: AsyncSession, *, code: str, percent: float, answers: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Write a code with its rules, publish with the note, commit; return the list.

    A code that is already there is replaced (rules and percent from these answers) and
    keeps its ``counted_from``; a new one is stamped now. The code is active.
    """
    promos = await upsert_draft_promo(
        session,
        code=code,
        percent=percent,
        active=True,
        rules=rules_from_answers(answers),
    )
    await commit_and_activate(session, notes=NOTE_PROMO_SAVED)
    return promos


async def toggle_promo_code(session: AsyncSession, code: str) -> list[dict[str, Any]]:
    """Flip a code's ``active`` (and only that), publish with the note, commit."""
    promos = await toggle_draft_promo_active(session, code)
    await commit_and_activate(session, notes=NOTE_PROMO_TOGGLED)
    return promos


async def delete_promo_code(session: AsyncSession, code: str) -> list[dict[str, Any]]:
    """Remove a code, publish with the note, commit. Creating it again starts its count at zero."""
    promos = await delete_draft_promo(session, code)
    await commit_and_activate(session, notes=NOTE_PROMO_DELETED)
    return promos


async def assert_promo_listed(session: AsyncSession, code: str) -> None:
    """Refuse a code the list does not hold (the delete's first step asks about it)."""
    view = await get_catalog_view(session)
    promos = list_promo_codes(view.version.pricing_document if view.version else None)
    if code.strip().upper() not in {str(p["code"]) for p in promos}:
        raise StoreAdminError(TEXT_PROMO_MISSING)


@dataclass(frozen=True)
class PromoLine:
    """What the list shows for one code. Plain text, not escaped: the caller escapes."""

    code: str
    percent: str
    active: bool
    rules: tuple[str, ...]
    usage: str

    @property
    def rules_text(self) -> str:
        return " · ".join(self.rules) or texts.admin_promo_rules_none


async def promo_lines(session: AsyncSession, promos: list[dict[str, Any]]) -> list[PromoLine]:
    """The code list's lines: percent, rules and the live usage the checks themselves count."""
    since = {str(row["code"]): row["counted_from"] for row in promos}
    usage = await live_usage_by_code(session, since=since) if promos else {}
    lines: list[PromoLine] = []
    for p in promos:
        code = str(p["code"])
        try:
            percent = to_persian_digits(f"{float(p['percent']):g}")
        except (TypeError, ValueError):
            percent = str(p["percent"])
        rules: list[str] = []
        if p["first_purchase_only"]:
            rules.append(texts.admin_promo_rule_first_only)
        elif p["per_user_limit"]:
            rules.append(
                texts.admin_promo_rule_per_user.format(n=to_persian_digits(p["per_user_limit"]))
            )
        if p["expires_at"] is not None:
            rules.append(
                texts.admin_promo_rule_expires.format(
                    date=format_jalali(p["expires_at"], persian_digits=True)
                )
            )
        used = to_persian_digits(usage.get(code, 0))
        if p["total_limit"]:
            usage_line = texts.admin_promo_usage.format(
                used=used, limit=to_persian_digits(p["total_limit"])
            )
        else:
            usage_line = texts.admin_promo_usage_unlimited.format(used=used)
        lines.append(
            PromoLine(
                code=code,
                percent=percent,
                active=bool(p.get("active", True)),
                rules=tuple(rules),
                usage=usage_line,
            )
        )
    return lines
