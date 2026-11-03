"""What the «🗓 هشدار و حذف سرویس» screen says, for the bot and for the Mini App.

The text of the bot's screen lived in its handler; the panel needs the same
words, so they are composed here and both call them. A caller prints what it is
given and decides nothing (``tests/unit/test_web_panel_settings.py`` checks the
page against ``screen_text`` line by line).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bot.texts import texts
from database.models.lifecycle import DELETE_MODE_OFF, DELETE_MODE_ON, DELETE_MODE_REHEARSAL
from services.lifecycle_settings import (
    DELETE_NUMBER_FIELDS,
    LIST_FIELDS,
    MAX_DAYS,
    MAX_PERCENT,
    REHEARSAL_VALID_DAYS,
    LifecycleConfig,
    rehearsal_is_fresh,
)
from services.ux_format import format_jalali, to_persian_digits

# The labels the screen prints, in its order: (field, label).
FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("warn_days", "هشدار روز"),
    ("warn_volume_percents", "هشدار حجم"),
    ("delete_after_expiry_days", "حذف بعد از انقضا"),
    ("delete_after_limited_days", "حذف بعد از اتمام حجم"),
    ("delete_trial_after_days", "حذف تست رایگان"),
    ("delete_warn_days_before", "هشدار پیش از حذف"),
)


def _fa(n: int) -> str:
    return to_persian_digits(n)


def _list_text(values: tuple[int, ...], template: str, suffix: str = "") -> str:
    if not values:
        return texts.admin_life_off
    joined = " و ".join(f"{_fa(v)}{suffix}" for v in values)
    return template.format(days=joined, percents=joined)


def _days_text(days: int, template: str = texts.admin_life_days_value) -> str:
    return texts.admin_life_off if days <= 0 else template.format(n=_fa(days))


def current_value_text(field: str, config: LifecycleConfig) -> str:
    """The setting's value as the owner reads it, for the screen and the prompt."""
    if field == "warn_days":
        return _list_text(config.warn_days, texts.admin_life_days_list)
    if field == "warn_volume_percents":
        return _list_text(config.warn_volume_percents, texts.admin_life_percent_list, "٪")
    value = int(getattr(config, field))
    if field == "delete_trial_after_days":
        return _days_text(value, texts.admin_life_trial_value)
    if field == "delete_warn_days_before":
        if value <= 0:
            return texts.admin_life_before_off
        return _days_text(value, texts.admin_life_before_value)
    return _days_text(value)


def mode_note(config: LifecycleConfig) -> str:
    return {
        DELETE_MODE_OFF: texts.admin_life_note_off,
        DELETE_MODE_REHEARSAL: texts.admin_life_note_rehearsal,
        DELETE_MODE_ON: texts.admin_life_note_on,
    }[config.delete_mode]


def rehearsal_text(config: LifecycleConfig) -> str:
    if config.last_rehearsal_at is None:
        return texts.admin_life_never_rehearsed
    return format_jalali(config.last_rehearsal_at, persian_digits=True)


def screen_text(config: LifecycleConfig) -> str:
    return texts.admin_life_screen.format(
        warn_days=current_value_text("warn_days", config),
        warn_volume=current_value_text("warn_volume_percents", config),
        mode=texts.admin_life_mode_names[config.delete_mode],
        after_expiry=current_value_text("delete_after_expiry_days", config),
        after_limited=current_value_text("delete_after_limited_days", config),
        trial=current_value_text("delete_trial_after_days", config),
        warn_before=current_value_text("delete_warn_days_before", config),
        rehearsal=rehearsal_text(config),
        note=mode_note(config),
    )


@dataclass(frozen=True)
class FieldView:
    field: str
    label: str
    value: str  # as the owner reads it
    raw: str  # what a form's box starts with
    form_label: str = ""  # the panel's short label for the box
    form_hint: str = ""  # the panel's hint under it: limits and exceptions, as the prompt states
    now_line: str = ""  # «الان: …»


@dataclass(frozen=True)
class ModeButton:
    mode: str
    label: str
    current: bool


@dataclass(frozen=True)
class LifecycleView:
    fields: tuple[FieldView, ...]
    mode: str
    mode_name: str
    note: str
    rehearsal: str
    rehearsal_fresh: bool
    valid_days: str  # how long a rehearsal unlocks «روشن», in Persian digits
    modes: tuple[ModeButton, ...]


def _raw_of(field: str, config: LifecycleConfig) -> str:
    if field in LIST_FIELDS:
        return " ".join(_fa(v) for v in getattr(config, field))
    assert field in DELETE_NUMBER_FIELDS
    return _fa(int(getattr(config, field)))


def lifecycle_view(config: LifecycleConfig, now: datetime) -> LifecycleView:
    fields = tuple(
        FieldView(
            field=field,
            label=label,
            value=current_value_text(field, config),
            raw=_raw_of(field, config),
            form_label=texts.web_life_fields[field][0],
            form_hint=texts.web_life_fields[field][1].format(
                max_days=_fa(MAX_DAYS), max_percent=_fa(MAX_PERCENT)
            ),
            now_line=texts.web_life_now.format(current=current_value_text(field, config)),
        )
        for field, label in FIELD_LABELS
    )
    labels = {DELETE_MODE_OFF: "خاموش", DELETE_MODE_REHEARSAL: "👁 تمرین", DELETE_MODE_ON: "روشن"}
    return LifecycleView(
        fields=fields,
        mode=config.delete_mode,
        mode_name=texts.admin_life_mode_names[config.delete_mode],
        note=mode_note(config).strip(),
        rehearsal=rehearsal_text(config),
        rehearsal_fresh=rehearsal_is_fresh(config, now),
        valid_days=_fa(REHEARSAL_VALID_DAYS),
        modes=tuple(
            ModeButton(mode=m, label=labels[m], current=config.delete_mode == m)
            for m in (DELETE_MODE_OFF, DELETE_MODE_REHEARSAL, DELETE_MODE_ON)
        ),
    )
