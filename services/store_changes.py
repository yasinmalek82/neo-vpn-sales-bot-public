"""«تغییرات فروشگاه»: the change log and its undo, one composition for the bot and the panel.

The log screen's text, the undo's confirm sentence (with how many later changes go
back too), the undo itself and its own change-log note were written inside
``bot/handlers/store_admin.py``. The Mini App shows the same screen, and a second
copy of «حواست باشه: N تغییر بعدی هم برمی‌گرده» is how the page would stop warning.
So they live here; the bot HTML-escapes through ``quote`` and the panel's Jinja
escapes on output.

**An entry is a version row.** ``ChangeEntry.version_id`` is the id of the
``StoreVersion`` row that edit produced. Publishing archives the row it replaces in
place (``publish_store_version`` changes its status, not its id) and only the clone
that becomes the new draft gets a new id, so the id of a log entry does not move
when later edits land. Only the pruning job deletes a row, so a pruned entry is
simply gone: a form that names it is refused (``StoreChangeGone``), never read as
another entry. The panel never identifies an entry by its place in the list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from services.store_admin import commit_and_activate
from services.store_edit import StaleStoreVersion, guarded_store_write
from services.store_strategy import ChangeEntry, list_store_changes, undo_store_change
from services.ux_format import format_relative_age, to_persian_digits

Quote = Callable[[str], str]

# The note an undo leaves, read back to the owner in the same log: an undo is an entry.
UNDO_NOTE = "برگشت: {description}"
# What a log row says when the entry is not in the visible list any more.
FALLBACK_DESCRIPTION = "تغییر در فروشگاه"
# The reasons an undo is refused although the form was current.
TEXT_NOT_UNDOABLE = "این اولین وضعیت فروشگاه است؛ قبل‌تر از این چیزی نیست."


def _same(text: str) -> str:
    return text


class StoreChangeGone(StaleStoreVersion):
    """The entry is not in the log any more (pruned, or never was); nothing was written."""

    def __init__(self) -> None:
        super().__init__(texts.admin_store_change_gone)


class StoreChangeNotUndoable(StaleStoreVersion):
    """The entry is the oldest state: there is nothing before it to go back to."""

    def __init__(self) -> None:
        super().__init__(TEXT_NOT_UNDOABLE)


@dataclass(frozen=True)
class ChangeRow:
    """One line of the log as printed: the marker, the note, how long ago."""

    version_id: int
    marker: str
    description: str
    when: str
    is_current: bool
    can_undo: bool


def change_rows(entries: list[ChangeEntry], *, now: datetime | None = None) -> list[ChangeRow]:
    """The log's rows, newest first, as the bot prints them («● note — ۵ دقیقه قبل»)."""
    return [
        ChangeRow(
            version_id=entry.version_id,
            marker="●" if entry.is_current else "○",
            description=entry.description,
            when=format_relative_age(entry.made_at, now=now),
            is_current=entry.is_current,
            can_undo=entry.can_undo,
        )
        for entry in entries
    ]


def change_log_text(
    entries: list[ChangeEntry], *, quote: Quote = _same, now: datetime | None = None
) -> str:
    """The bot's whole «تغییرات فروشگاه» message (title, one line per entry, the hint)."""
    if not entries:
        return texts.admin_store_changes_empty
    lines = [texts.admin_store_changes_title]
    for row in change_rows(entries, now=now):
        lines.append(f"{row.marker} {quote(row.description)} — {row.when}")
    lines.append("")
    lines.append(texts.admin_store_changes_hint)
    return "\n".join(lines)


def undo_confirm_text(entry: ChangeEntry, *, quote: Quote = _same) -> str:
    """The sentence before an undo, and the count of later changes that go back with it."""
    body = texts.admin_store_undo_confirm.format(description=quote(entry.description))
    if entry.undoes_also:
        also = texts.admin_store_undo_also.format(count=to_persian_digits(entry.undoes_also))
        body = f"{body}\n\n{also}"
    return body


def undone_text(description: str, *, quote: Quote = _same) -> str:
    """What the owner reads after an undo went through."""
    return texts.admin_store_undone_body.format(description=quote(description))


async def apply_undo(session: AsyncSession, version_id: int) -> str:
    """Undo the entry, log the undo as its own entry, commit. Returns the entry's note.

    ``undo_store_change`` restores the predecessor of the entry into a draft;
    ``commit_and_activate`` publishes it with «برگشت: …», so the undo can be undone in
    turn. The caller rolls back on ``StoreAdminError``.
    """
    entries = await list_store_changes(session)
    entry = next((e for e in entries if e.version_id == version_id), None)
    description = entry.description if entry else FALLBACK_DESCRIPTION
    await undo_store_change(session, version_id)
    await commit_and_activate(session, notes=UNDO_NOTE.format(description=description))
    return description


async def find_undoable(session: AsyncSession, version_id: int) -> ChangeEntry:
    """The listed entry the panel may offer to undo, or the refusal (stale, nothing written)."""
    entries = await list_store_changes(session)
    entry = next((e for e in entries if e.version_id == version_id), None)
    if entry is None:
        raise StoreChangeGone
    if not entry.can_undo:
        raise StoreChangeNotUndoable
    return entry


async def ask_undo(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    version_id: int,
) -> ChangeEntry:
    """The confirm step: the entry the owner is about to undo. Writes nothing."""

    async def action(s: AsyncSession) -> ChangeEntry:
        entry = await find_undoable(s, version_id)
        await s.rollback()  # asking changes nothing: not even a draft is left behind
        return entry

    return await guarded_store_write(
        session, settings, telegram_id, expected_version=expected_version, action=action
    )


async def confirm_undo(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    version_id: int,
) -> str:
    """The panel's undo: owner only, current version only, only a listed entry.

    Returns the entry's note. The same ``apply_undo`` the bot's confirm button runs.
    """

    async def action(s: AsyncSession) -> str:
        await find_undoable(s, version_id)
        return await apply_undo(s, version_id)

    return await guarded_store_write(
        session, settings, telegram_id, expected_version=expected_version, action=action
    )


__all__ = [
    "FALLBACK_DESCRIPTION",
    "TEXT_NOT_UNDOABLE",
    "UNDO_NOTE",
    "ChangeRow",
    "StoreChangeGone",
    "StoreChangeNotUndoable",
    "apply_undo",
    "ask_undo",
    "change_log_text",
    "change_rows",
    "confirm_undo",
    "find_undoable",
    "undo_confirm_text",
    "undone_text",
]
