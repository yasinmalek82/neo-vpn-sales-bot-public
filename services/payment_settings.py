"""The card customers pay to: DB value per field, else env (P4).

Same shape as ``services/panel_settings.py``. The owner edits the card from the
store hub; a field left unset falls back to ``PAYMENT_*`` in ``.env``, so a shop
that never opens the screen behaves exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from database.models.payment_settings import PAYMENT_SETTINGS_ROW_ID, PaymentSettings

CARD_DIGITS = 16
HOLDER_MAX = 64
BANK_MAX = 64
INSTRUCTIONS_MAX = 300

FIELDS = ("card_number", "card_holder_name", "bank_name", "instructions")
_LIMITS = {"card_holder_name": HOLDER_MAX, "bank_name": BANK_MAX, "instructions": INSTRUCTIONS_MAX}

_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})
# Bidi / zero-width marks a phone inserts when a number is pasted from a chat,
# plus every dash and space variant.
_STRIP = re.compile(
    "[\\s\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\u00a0\\-\u2010-\u2015\u2212\u0640]"
)


class PaymentSettingsError(ValueError):
    """Bad input for a payment field. ``code`` is machine-readable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PaymentInfo:
    card_number: str
    card_holder_name: str
    bank_name: str
    instructions: str
    from_env: frozenset[str]
    has_override: bool


def normalize_card_number(raw: str) -> str:
    """ASCII digits only; exactly 16 or ``PaymentSettingsError('card_invalid')``."""
    cleaned = _STRIP.sub("", (raw or "").translate(_DIGIT_MAP))
    if len(cleaned) != CARD_DIGITS or not (cleaned.isascii() and cleaned.isdigit()):
        raise PaymentSettingsError("card_invalid")
    return cleaned


def format_card_number(digits: str) -> str:
    """«6037991234567890» → «6037 9912 3456 7890»; anything else is returned as is."""
    if len(digits) == CARD_DIGITS and digits.isdigit():
        return " ".join(digits[i : i + 4] for i in range(0, CARD_DIGITS, 4))
    return digits


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def get_settings_row(session: AsyncSession) -> PaymentSettings | None:
    result = await session.execute(
        select(PaymentSettings).where(PaymentSettings.id == PAYMENT_SETTINGS_ROW_ID)
    )
    return result.scalar_one_or_none()


async def get_payment_info(session: AsyncSession, settings: Settings) -> PaymentInfo:
    """Each field: DB value if set, else env. ``from_env`` names the env-sourced ones."""
    row = await get_settings_row(session)
    env = {
        "card_number": settings.payment_card_number,
        "card_holder_name": settings.payment_card_holder_name,
        "bank_name": settings.payment_bank_name,
        "instructions": settings.payment_instructions,
    }
    values: dict[str, str] = {}
    from_env: set[str] = set()
    overridden = False
    for name in FIELDS:
        db_value = _nonempty(getattr(row, name, None)) if row is not None else None
        if db_value is not None:
            values[name] = db_value
            overridden = True
        else:
            values[name] = str(env[name] or "")
            from_env.add(name)
    return PaymentInfo(
        card_number=values["card_number"],
        card_holder_name=values["card_holder_name"],
        bank_name=values["bank_name"],
        instructions=values["instructions"],
        from_env=frozenset(from_env),
        has_override=overridden,
    )


async def _ensure_row(session: AsyncSession) -> PaymentSettings:
    row = await get_settings_row(session)
    if row is None:
        row = PaymentSettings(id=PAYMENT_SETTINGS_ROW_ID)
        session.add(row)
        await session.flush()
    return row


def validate_field(field: str, raw: str) -> str:
    """The value that would be stored, or ``PaymentSettingsError``."""
    if field not in FIELDS:
        raise PaymentSettingsError("unknown_field")
    if field == "card_number":
        return normalize_card_number(raw)
    value = (raw or "").strip()
    if not value:
        raise PaymentSettingsError("empty")
    if len(value) > _LIMITS[field]:
        raise PaymentSettingsError("too_long")
    return value


async def save_payment_field(
    session: AsyncSession, field: str, raw: str, *, admin_user_id: int | None = None
) -> PaymentSettings:
    """Validate and upsert one field. The caller commits."""
    value = validate_field(field, raw)
    row = await _ensure_row(session)
    setattr(row, field, value)
    row.updated_at = datetime.now(UTC)
    row.updated_by_admin_id = admin_user_id
    await session.flush()
    return row


async def clear_to_env(session: AsyncSession, *, admin_user_id: int | None = None) -> None:
    """Drop every override so the .env values are used again."""
    row = await get_settings_row(session)
    if row is None:
        return
    for name in FIELDS:
        setattr(row, name, None)
    row.updated_at = datetime.now(UTC)
    row.updated_by_admin_id = admin_user_id
    await session.flush()
