"""What a person meets when «database is locked» does happen.

Live: the report-group attach died on ``UPDATE report_settings`` after 19
seconds and the owner read «یه مشکلی پیش اومد». The cause is fixed elsewhere;
this pins what the error path itself must guarantee, whichever handler hits
it next: the apology, nothing half-written, and a retry that simply works
because the conversation is still where it was.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from bot.handlers import admin as admin_handlers
from bot.states import AdminStates
from bot.texts import texts
from database.models import User
from database.models.report import ReportSettings
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App, plain

GROUP = -1001234567890


def _locked() -> OperationalError:
    return OperationalError(
        "UPDATE report_settings SET topics=?", {}, sqlite3.OperationalError("database is locked")
    )


async def test_a_locked_database_apologises_writes_nothing_and_leaves_the_retry_open(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "⚙️")
    await tap_starting(admin, "📡")
    await tap_starting(admin, "🔗 وصل کردن گروه")
    chat = app.tg.add_forum(GROUP)
    real_setup = admin_handlers.run_full_setup

    async def half_done_then_locked(session: Any, bot: Any, *, chat_id: int) -> Any:
        _ = bot, chat_id
        # A write that reached the database but not its commit...
        session.add(User(telegram_id=424242, first_name="half", referral_code="HALF0001"))
        await session.flush()
        # ...and then the write SQLite refuses.
        raise _locked()

    monkeypatch.setattr(admin_handlers, "run_full_setup", half_done_then_locked)
    app.expect_unhandled_errors()
    await admin.send(str(GROUP))

    assert plain(texts.error_generic) in admin.last_text
    assert "database is locked" not in admin.last_text and "OperationalError" not in admin.last_text
    assert any("database is locked" in error for error in app.unhandled_errors)
    async with app.session() as session:
        half = await session.scalar(
            select(func.count()).select_from(User).where(User.telegram_id == 424242)
        )
        attached = await session.scalar(select(ReportSettings.chat_id))
    assert half == 0, "the failed update left a row behind"
    assert attached is None
    state = app.dp.fsm.resolve_context(app.bot, chat_id=admin.user_id, user_id=admin.user_id)
    assert await state.get_state() == AdminStates.waiting_report_chat_id.state

    # The retry is the same message again: no menu to climb back through.
    monkeypatch.setattr(admin_handlers, "run_full_setup", real_setup)
    await admin.send(str(GROUP))
    assert "آماده شد" in admin.last_text
    assert len(chat.topics) == 8
