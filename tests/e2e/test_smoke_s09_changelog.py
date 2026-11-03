"""Section ۹, part 3: the store change log and undo.

Rows ۹.۱۶ to ۹.۲۱.
STALE: ۹.۱۶ says «🔧 پیشرفته → تغییرات فروشگاه»; the log is «↩️ تغییرات» on the store hub.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from database.models.store import StoreVersion
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s09_helpers import drafts, live_plans, open_store, shelf
from tests.e2e.harness import Actor, App, Store, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

REMOVED = "پلن «۵۰ گیگ / ۳۰ روز» حذف شد"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _do_three_edits(admin: Actor) -> None:
    """Rename a group, change a price, delete a plan: three edits on top of the seed."""
    await open_store(admin)
    await tap_starting(admin, "حجمی")
    await tap_starting(admin, "ویژه")
    await tap_starting(admin, "✏️ گروه")
    await tap_starting(admin, "✏️ عنوان")
    await admin.send("ویژه‌تر")
    await tap_starting(admin, "⬅️ گروه")
    await tap_starting(admin, "۱۰۰ گیگ")
    await tap_starting(admin, "💰 قیمت")
    await admin.send("400000")
    await tap_starting(admin, "⬅️ گروه")
    await tap_starting(admin, "۵۰ گیگ")
    await tap_starting(admin, "🗑 حذف این پلن")
    await tap_starting(admin, "🗑 بله، حذف کن")


async def _open_log(admin: Actor) -> None:
    await admin.send("/admin")
    await tap_starting(admin, "🛍")
    await tap_starting(admin, "↩️ تغییرات")


def _entries(admin: Actor) -> list[str]:
    """The log lines, newest first, as the owner reads them."""
    return [
        ln
        for ln in admin.last_text.splitlines()
        if ln and ln[0] in "●○" and not ln.startswith("● یعنی")
    ]


@smoke("۹.۱۶")
async def test_the_log_lists_the_owners_edits_with_relative_time_not_version_numbers(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    versions = await app.all(select(StoreVersion).order_by(StoreVersion.version))
    assert len(versions) >= 4
    # Spread the edits over time so «relative» means something.
    for age, v in zip((300, 120, 30, 0), versions[-4:], strict=True):
        if age:
            await app.age(
                StoreVersion,
                v.id,
                minutes=age,
                columns=("published_at", "updated_at", "created_at"),
            )
    await _open_log(admin)
    lines = _entries(admin)
    assert len(lines) == 4, lines
    assert lines[0].startswith("●") and all(ln.startswith("○") for ln in lines[1:])
    assert report_plain(REMOVED) in lines[0] and "همین الان" in lines[0]
    assert "30 دقیقه قبل" in lines[1] and "120 دقیقه قبل" in lines[2] or "ساعت" in lines[2]
    assert "کاتالوگ آزمون" in lines[3]
    shown = admin.last_text
    assert not re.search(r"\bv\d+\b", shown) and "بایگانی" not in shown and "archived" not in shown
    assert "● یعنی وضعیت فعلی" in shown
    assert await drafts(app) == 0


@smoke("۹.۱۷")
async def test_undoing_the_newest_change_does_not_mention_later_changes(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    await _open_log(admin)
    await tap_starting(admin, f"↩️ ● {REMOVED}")
    assert "فروشگاه به قبل از این تغییر برگرده؟" in admin.last_text
    assert report_plain(REMOVED) in admin.last_text
    assert "تغییر بعدی" not in admin.last_text and "حواست باشه" not in admin.last_text
    assert "✅ برگردان" in " ".join(admin.buttons)


@smoke("۹.۱۸")
async def test_confirming_brings_the_deleted_plan_back_for_the_customer_at_once(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    assert "۵۰ گیگ / ۳۰ روز" not in await live_plans(app, "ویژه‌تر")
    await _open_log(admin)
    await tap_starting(admin, f"↩️ ● {REMOVED}")
    await tap_starting(admin, "✅ برگردان")
    assert "برگشت و همین الان برای مشتری فعاله" in admin.last_text
    assert "۵۰ گیگ / ۳۰ روز" in await live_plans(app, "ویژه‌تر")
    ali = app.customer("ali")
    await ali.start()
    labels = await shelf(ali, "حجمی", "ویژه‌تر")
    assert any("۵۰ گیگ" in b for b in labels), labels
    assert await drafts(app) == 0


@smoke("۹.۱۹")
async def test_the_undo_itself_is_logged_and_can_be_undone_in_turn(app: App, store: Store) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    await _open_log(admin)
    before = len(_entries(admin))
    await tap_starting(admin, f"↩️ ● {REMOVED}")
    await tap_starting(admin, "✅ برگردان")
    await _open_log(admin)
    lines = _entries(admin)
    assert len(lines) == before + 1, lines
    assert lines[0].startswith("●") and report_plain(f"برگشت: {REMOVED}") in lines[0]
    assert all(ln.startswith("○") for ln in lines[1:])
    # Undoing the undo puts the deletion back.
    await tap_starting(admin, "↩️ ● برگشت")
    await tap_starting(admin, "✅ برگردان")
    assert "۵۰ گیگ / ۳۰ روز" not in await live_plans(app, "ویژه‌تر")


@smoke("۹.۲۰")
async def test_an_older_change_says_how_many_later_changes_go_with_it(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    await _open_log(admin)
    lines = _entries(admin)
    # Every undoable row: the number it announces is the number of rows above it.
    for position in range(1, len(lines) - 1):
        await _open_log(admin)
        label = next(
            b
            for b in admin.buttons
            if b.startswith("↩️")
            and report_plain(b.split("↩️ ", 1)[1].lstrip("● ")) in lines[position]
        )
        await tap_starting(admin, label)
        found = re.search(r"([0-9]+) تغییر بعدی هم", admin.last_text)
        assert found, admin.last_text
        assert int(found.group(1).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))) == position
    # Confirming really takes the later edits with it: the rename is gone too.
    await _open_log(admin)
    await tap_starting(admin, "↩️ گروه محصول ویرایش شد")
    await tap_starting(admin, "✅ برگردان")
    assert await live_plans(app, "ویژه") == ["۵۰ گیگ / ۳۰ روز", "۱۰۰ گیگ / ۹۰ روز"]


@smoke("۹.۲۱")
async def test_the_oldest_state_has_no_undo_button(app: App, store: Store) -> None:
    admin = app.admin()
    await _do_three_edits(admin)
    await _open_log(admin)
    lines = _entries(admin)
    assert "کاتالوگ آزمون" in lines[-1]
    undo_buttons = [b for b in admin.buttons if b.startswith("↩️")]
    assert len(undo_buttons) == len(lines) - 1, admin.buttons
    assert "کاتالوگ آزمون" in admin.buttons, "the oldest row is shown, just not undoable"
    assert not any("کاتالوگ آزمون" in b for b in undo_buttons)
    # A forged tap on it (a stale screen) is answered, and changes nothing.
    from bot.keyboards.store_admin import StoreCB

    version = (await app.all(select(StoreVersion).order_by(StoreVersion.version)))[0]
    after = await live_plans(app, "ویژه‌تر")
    await admin.press(f"{StoreCB.HIST_OK}{version.id}")
    assert await live_plans(app, "ویژه‌تر") == after, "the oldest state was restored"
    assert "اولین وضعیت" in (admin.popup or ""), admin.popup
