"""The owner's numbers for warnings and for deleting ended services.

Read by the jobs (``scheduler/jobs/renewal_reminders.py``, ``volume_warnings.py``,
``service_lifecycle.py``) and edited from «⚙️ تنظیمات» → «🗓 هشدار و حذف سرویس».

* **No row means the defaults, and reading never writes.** A quiet shop's idle
  jobs must not commit (``tests/integration/test_idle_jobs_read_only.py``); the
  row is created by the first save from the screen. ``RENEWAL_REMINDER_DAYS``
  only seeds the day list of that first row.
* **«روشن» is refused here, not only on the screen**, unless a rehearsal
  reported within the last ``REHEARSAL_VALID_DAYS`` days. Changing a deletion
  number voids the rehearsal: it listed what the OLD numbers would delete.
* Typed values are checked where they are typed and the message states the
  range. Admin copy.
* **Both callers write through here** (the bot's typed prompt and the Mini App's
  form): the field-to-parser choice (``parse_field``), the owner rule, the
  staleness guard (``guarded_lifecycle_write``) and the «روشن» precondition all
  live in this module, so a caller cannot skip one.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.lifecycle import (
    DEFAULT_DELETE_AFTER_EXPIRY_DAYS,
    DEFAULT_DELETE_AFTER_LIMITED_DAYS,
    DEFAULT_DELETE_TRIAL_AFTER_DAYS,
    DEFAULT_DELETE_WARN_DAYS_BEFORE,
    DEFAULT_WARN_VOLUME_PERCENTS,
    DELETE_MODE_OFF,
    DELETE_MODE_ON,
    DELETE_MODE_REHEARSAL,
    DELETE_MODES,
    LIFECYCLE_SETTINGS_ROW_ID,
    LifecycleSettings,
)
from services.store_config import is_store_owner
from services.ux_format import to_persian_digits

# A rehearsal older than this does not unlock «روشن».
REHEARSAL_VALID_DAYS = 7

MAX_DAYS = 365
MAX_PERCENT = 99

LIST_FIELDS = ("warn_days", "warn_volume_percents")
# Changing one of these voids the rehearsal.
DELETE_NUMBER_FIELDS = (
    "delete_after_expiry_days",
    "delete_after_limited_days",
    "delete_trial_after_days",
    "delete_warn_days_before",
)
NUMBER_FIELDS = (*LIST_FIELDS, *DELETE_NUMBER_FIELDS)

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_SEPARATORS = re.compile(r"[\s,،؛;]+")
_TOKEN = re.compile(r"\d+")


class SettingsError(ValueError):
    """A refused value or change. The message is admin copy, safe to show as it is."""


class LifecycleOwnerRequired(SettingsError):
    """The numbers decide what is deleted from the panel: only the store owner changes them."""


class StaleLifecycleSettings(SettingsError):
    """The settings changed since the form was drawn; nothing was written."""


TEXT_STALE = "تنظیمات در این فاصله عوض شد. چیزی ذخیره نشد؛ صفحه را دوباره باز کن."


@dataclass(frozen=True)
class LifecycleConfig:
    """What the jobs read. Plain data: no ORM row crosses a network await."""

    warn_days: tuple[int, ...]
    warn_volume_percents: tuple[int, ...]
    delete_mode: str
    delete_after_expiry_days: int
    delete_after_limited_days: int
    delete_trial_after_days: int
    delete_warn_days_before: int
    last_rehearsal_at: datetime | None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _normalized(values: Iterable[int]) -> list[int]:
    return sorted({int(v) for v in values}, reverse=True)


def default_config(seed_days: Sequence[int]) -> LifecycleConfig:
    return LifecycleConfig(
        warn_days=tuple(_normalized(d for d in seed_days if int(d) >= 0)),
        warn_volume_percents=tuple(DEFAULT_WARN_VOLUME_PERCENTS),
        delete_mode=DELETE_MODE_OFF,
        delete_after_expiry_days=DEFAULT_DELETE_AFTER_EXPIRY_DAYS,
        delete_after_limited_days=DEFAULT_DELETE_AFTER_LIMITED_DAYS,
        delete_trial_after_days=DEFAULT_DELETE_TRIAL_AFTER_DAYS,
        delete_warn_days_before=DEFAULT_DELETE_WARN_DAYS_BEFORE,
        last_rehearsal_at=None,
    )


def _config_of(row: LifecycleSettings) -> LifecycleConfig:
    return LifecycleConfig(
        warn_days=tuple(_normalized(row.warn_days or [])),
        warn_volume_percents=tuple(_normalized(row.warn_volume_percents or [])),
        delete_mode=row.delete_mode if row.delete_mode in DELETE_MODES else DELETE_MODE_OFF,
        delete_after_expiry_days=int(row.delete_after_expiry_days),
        delete_after_limited_days=int(row.delete_after_limited_days),
        delete_trial_after_days=int(row.delete_trial_after_days),
        delete_warn_days_before=int(row.delete_warn_days_before),
        last_rehearsal_at=_aware(row.last_rehearsal_at),
    )


async def load_config(session: AsyncSession, *, seed_days: Sequence[int]) -> LifecycleConfig:
    """The current numbers. Never writes: a missing row is the defaults."""
    row = await session.get(LifecycleSettings, LIFECYCLE_SETTINGS_ROW_ID)
    if row is None:
        return default_config(seed_days)
    return _config_of(row)


async def _row_for_update(session: AsyncSession, seed_days: Sequence[int]) -> LifecycleSettings:
    row = await session.get(LifecycleSettings, LIFECYCLE_SETTINGS_ROW_ID)
    if row is not None:
        return row
    seed = default_config(seed_days)
    row = LifecycleSettings(
        id=LIFECYCLE_SETTINGS_ROW_ID,
        warn_days=list(seed.warn_days),
        warn_volume_percents=list(seed.warn_volume_percents),
        delete_mode=seed.delete_mode,
        delete_after_expiry_days=seed.delete_after_expiry_days,
        delete_after_limited_days=seed.delete_after_limited_days,
        delete_trial_after_days=seed.delete_trial_after_days,
        delete_warn_days_before=seed.delete_warn_days_before,
    )
    session.add(row)
    await session.flush()
    return row


# --- typed values -----------------------------------------------------------


def _tokens(raw: str) -> list[str]:
    text = (raw or "").translate(_DIGITS).strip()
    if not text:
        return []
    return [t for t in _SEPARATORS.split(text) if t]


def _to_int(token: str) -> int:
    """``int()`` of a digit string, without Python's 4300-digit limit raising.

    Anything longer than 18 digits (after leading zeros) is past every range here
    and past a 64-bit column, so it is returned as a number that is simply too big.
    """
    digits = token.lstrip("0") or "0"
    return int(digits) if len(digits) <= 18 else 10**18


def _list_error(unit: str, high: int) -> SettingsError:
    return SettingsError(
        f"فقط عددهای صحیح بین ۱ تا {to_persian_digits(high)} {unit}، با فاصله بینشان "
        "(مثلاً «۳ ۱»). ۰ یا خالی یعنی خاموش."
    )


def _parse_list(raw: str, *, high: int, unit: str) -> list[int]:
    tokens = _tokens(raw)
    if any(not _TOKEN.fullmatch(t) for t in tokens):
        raise _list_error(unit, high)
    numbers = [_to_int(t) for t in tokens]
    if not numbers or numbers == [0] * len(numbers):
        return []
    if 0 in numbers or any(n > high for n in numbers):
        # A zero among real numbers is ambiguous («خاموش» or «روز آخر»?): refuse it.
        raise _list_error(unit, high)
    return _normalized(numbers)


def parse_day_list(raw: str) -> list[int]:
    """«۳ ۱» -> [3, 1]. ۰ or empty -> [] (off). Days 1..365."""
    return _parse_list(raw, high=MAX_DAYS, unit="روز")


def parse_percent_list(raw: str) -> list[int]:
    """«۲۰ ۵» -> [20, 5]. ۰ or empty -> [] (off). Percents 1..99."""
    return _parse_list(raw, high=MAX_PERCENT, unit="درصد")


def parse_days_number(raw: str) -> int:
    """One number of days, 0..365; 0 turns that rule off."""
    tokens = _tokens(raw)
    if len(tokens) != 1 or not _TOKEN.fullmatch(tokens[0]) or _to_int(tokens[0]) > MAX_DAYS:
        raise SettingsError(
            f"یک عدد صحیح بین ۰ تا {to_persian_digits(MAX_DAYS)} روز بنویس. ۰ یعنی خاموش."
        )
    return _to_int(tokens[0])


def parse_field(field: str, raw: str) -> list[int] | int:
    """The typed text of one setting, parsed the way its screen parses it.

    The bot's prompt and the panel's form both call this, so the field-to-parser
    choice exists once. ``SettingsError`` carries the range in admin copy.
    """
    if field == "warn_days":
        return parse_day_list(raw)
    if field == "warn_volume_percents":
        return parse_percent_list(raw)
    if field in DELETE_NUMBER_FIELDS:
        return parse_days_number(raw)
    raise SettingsError("این تنظیم وجود ندارد.")


# --- saving -----------------------------------------------------------------


async def save_number(
    session: AsyncSession, *, seed_days: Sequence[int], field: str, value: Any
) -> None:
    """Store one setting. The caller commits. Re-checks the range, so a forged
    value cannot get past the screen."""
    if field not in NUMBER_FIELDS:
        raise SettingsError("این تنظیم وجود ندارد.")
    if field == "warn_days":
        stored: Any = _checked_list(value, high=MAX_DAYS, unit="روز")
    elif field == "warn_volume_percents":
        stored = _checked_list(value, high=MAX_PERCENT, unit="درصد")
    else:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_DAYS:
            raise SettingsError(
                f"یک عدد صحیح بین ۰ تا {to_persian_digits(MAX_DAYS)} روز بنویس. ۰ یعنی خاموش."
            )
        stored = int(value)
    row = await _row_for_update(session, seed_days)
    setattr(row, field, stored)
    if field in DELETE_NUMBER_FIELDS:
        # The rehearsal listed what the old numbers would delete. And deleting does not stay
        # ON with numbers nobody rehearsed: it goes back to «تمرین» in the same flush.
        row.last_rehearsal_at = None
        if row.delete_mode == DELETE_MODE_ON:
            row.delete_mode = DELETE_MODE_REHEARSAL
    await session.flush()


def _checked_list(value: Any, *, high: int, unit: str) -> list[int]:
    if not isinstance(value, list) or any(
        not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= high for v in value
    ):
        raise _list_error(unit, high)
    return _normalized(value)


def rehearsal_is_fresh(config: LifecycleConfig, now: datetime) -> bool:
    last = config.last_rehearsal_at
    if last is None:
        return False
    return timedelta(0) <= now - last <= timedelta(days=REHEARSAL_VALID_DAYS)


def require_fresh_rehearsal(config: LifecycleConfig, now: datetime) -> None:
    """The one precondition of real deletion; every way to «روشن» passes through it."""
    if not rehearsal_is_fresh(config, now):
        raise SettingsError(
            "روشن کردن حذف خودکار رد شد: اول باید یک بار «تمرین» اجرا بشه و گزارشش بیاد "
            f"(حداکثر {to_persian_digits(REHEARSAL_VALID_DAYS)} روز پیش). "
            "تمرین چیزی رو حذف نمی‌کنه و فقط می‌نویسه چی حذف می‌شد. "
            "اگه بعد از تمرین عددی رو عوض کردی، دوباره تمرین لازمه."
        )


async def set_delete_mode(
    session: AsyncSession,
    *,
    seed_days: Sequence[int],
    mode: str,
    now: datetime | None = None,
) -> None:
    """Switch the deletion mode. The caller commits.

    «روشن» needs a rehearsal that reported within the last seven days, and the
    refusal says so. Going to «خاموش» or «تمرین» is always allowed.
    """
    if mode not in DELETE_MODES:
        raise SettingsError("این حالت وجود ندارد.")
    moment = now or datetime.now(UTC)
    if mode == DELETE_MODE_ON:
        current = await load_config(session, seed_days=seed_days)
        require_fresh_rehearsal(current, moment)
    row = await _row_for_update(session, seed_days)
    if row.delete_mode == DELETE_MODE_OFF and mode != DELETE_MODE_OFF:
        # «limited for N days» was only tracked while the job ran; after any pause (a renewal
        # in between, the bot down) the old clocks mean nothing. Every one starts again.
        from sqlalchemy import update as _update

        from database.models import PasarGuardAccount

        await session.execute(_update(PasarGuardAccount).values(limited_since=None))
    row.delete_mode = mode
    await session.flush()


async def record_rehearsal(
    session: AsyncSession, *, seed_days: Sequence[int], now: datetime
) -> None:
    """A rehearsal ran and reported. The caller commits."""
    row = await _row_for_update(session, seed_days)
    row.last_rehearsal_at = now
    await session.flush()


# --- the panel's guarded writes ----------------------------------------------
#
# The bot protects a typed value with its FSM state (cleared after the message).
# A web form has no such state, and a phone with a weak connection resends. So a
# form carries the stamp of the row it was drawn from and a write is refused when
# the row moved meanwhile: a double-submitted form applies once and a mode switch
# never flips back. The stamp is the row's ``updated_at`` (no migration); a shop
# with no row yet stamps ``"none"``, and the first save creates the row.

_T = TypeVar("_T")

NO_ROW_STAMP = "none"


def can_edit(settings: Settings, telegram_id: int | None) -> bool:
    """The numbers decide what is deleted from the panel: the owner changes them."""
    return is_store_owner(settings, telegram_id)


def assert_lifecycle_owner(settings: Settings, telegram_id: int | None) -> None:
    if not can_edit(settings, telegram_id):
        raise LifecycleOwnerRequired(texts.admin_life_not_owner)


async def current_stamp(session: AsyncSession, *, lock: bool = False) -> str:
    """What a form is drawn from. ``lock`` is for the write path (``FOR UPDATE`` on a
    server database; SQLite ignores it and refuses the later writer instead)."""
    stmt = select(LifecycleSettings.updated_at).where(
        LifecycleSettings.id == LIFECYCLE_SETTINGS_ROW_ID
    )
    moment = await session.scalar(stmt.with_for_update() if lock else stmt)
    stamped = _aware(moment)
    return NO_ROW_STAMP if stamped is None else stamped.isoformat()


async def assert_stamp(session: AsyncSession, expected: str | None, *, lock: bool) -> None:
    if expected is None or await current_stamp(session, lock=lock) != expected:
        raise StaleLifecycleSettings(TEXT_STALE)


async def guarded_lifecycle_write(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_stamp: str | None,
    action: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    """Every panel write here: the store owner only, the row as the form saw it.

    ``action`` runs in the transaction that read the stamp; this commits it. Any
    refusal rolls back, so nothing half-written stays. SQLite's «database is
    locked» (a concurrent writer passed our snapshot) is that same stale refusal.
    """
    assert_lifecycle_owner(settings, telegram_id)
    try:
        await assert_stamp(session, expected_stamp, lock=True)
        result = await action(session)
        await session.commit()
        return result
    except SettingsError:
        await session.rollback()
        raise
    except IntegrityError as exc:
        # Two first saves of a shop with no row yet: the loser meets the winner's row.
        await session.rollback()
        raise StaleLifecycleSettings(TEXT_STALE) from exc
    except OperationalError as exc:
        await session.rollback()
        if "locked" in str(exc).lower():
            raise StaleLifecycleSettings(TEXT_STALE) from exc
        raise


def saved_line(field: str, *, was_on: bool) -> str:
    """The bot's «✅ ذخیره شد» and, for a deletion number, what it did to the mode."""
    line = texts.admin_life_saved
    if field in DELETE_NUMBER_FIELDS:
        line += "\n" + (
            texts.admin_life_back_to_rehearsal if was_on else texts.admin_life_rehearsal_voided
        )
    return line


async def save_typed_number(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_stamp: str | None,
    field: str,
    raw: str,
) -> str:
    """The panel's save of one setting: guard, parse, ``save_number``. Returns the saved line."""

    async def action(s: AsyncSession) -> str:
        before = await load_config(s, seed_days=settings.renewal_reminder_days)
        value = parse_field(field, raw)
        await save_number(s, seed_days=settings.renewal_reminder_days, field=field, value=value)
        return saved_line(field, was_on=before.delete_mode == DELETE_MODE_ON)

    return await guarded_lifecycle_write(
        session, settings, telegram_id, expected_stamp=expected_stamp, action=action
    )


async def ask_delete_on(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_stamp: str | None,
    now: datetime | None = None,
) -> bool:
    """The step before the confirm page: writes nothing.

    Owner and current form first, then the same precondition ``set_delete_mode``
    enforces (so the owner is told why up front, not after pressing «yes»).
    Returns True when deletion is already on (nothing to confirm).
    """
    assert_lifecycle_owner(settings, telegram_id)
    await assert_stamp(session, expected_stamp, lock=False)
    config = await load_config(session, seed_days=settings.renewal_reminder_days)
    if config.delete_mode == DELETE_MODE_ON:
        return True
    require_fresh_rehearsal(config, now or datetime.now(UTC))
    return False


async def switch_delete_mode(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_stamp: str | None,
    mode: str,
    now: datetime | None = None,
) -> None:
    """The panel's mode switch: guard, then ``set_delete_mode`` (which refuses «روشن»
    without a fresh rehearsal, whoever calls it)."""

    async def action(s: AsyncSession) -> None:
        await set_delete_mode(s, seed_days=settings.renewal_reminder_days, mode=mode, now=now)

    await guarded_lifecycle_write(
        session, settings, telegram_id, expected_stamp=expected_stamp, action=action
    )
