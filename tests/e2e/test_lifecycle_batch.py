"""Section ۱۶, rows ۱۶.۲۳ to ۱۶.۲۸: the list the owner approves by hand.

A run that would delete more than twenty services (or a quarter of them, over
five) deletes nothing and stores the list. The owner opens it, looks at a
service he doubts, exempts it, and approves what is left. Every id is re-read
and re-decided right before its delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.keyboards.lifecycle import LifeCB
from database.models.lifecycle import LifecycleBatch
from tests.e2e._s16_helpers import (
    age_delete_warnings,
    configure,
    is_deleted,
    open_lifecycle,
    panel_deletes,
    run_lifecycle,
    seed_service,
    sqlite_exec,
)
from tests.e2e.harness import App, smoke

N = 25


async def _backlog(app: App, count: int = N) -> object:
    """``count`` paid-looking services ended 15 days ago, owned by one customer."""
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    for i in range(count):
        await seed_service(
            app,
            ali.user_id,
            f"bk_{i:02d}",
            status="expired",
            expires_in_days=-15,
            label=f"خدمت {i:02d}",
        )
    return ali


async def _stopped(app: App, **config: object) -> tuple[object, object]:
    group = await app.connect_report_group()
    ali = await _backlog(app)
    await configure(app, delete_mode="on", delete_warn_days_before=0, **config)
    await run_lifecycle(app)
    await app.run_job("report_flush")
    return ali, group


def _rows(admin: object, text: str = "خدمت") -> list[str]:
    return [b for b in admin.buttons if text in b]  # type: ignore[attr-defined]


async def _batches(app: App) -> list[LifecycleBatch]:
    from sqlalchemy import select

    async with app.session() as session:
        return list((await session.execute(select(LifecycleBatch))).scalars().all())


@smoke("۱۶.۲۳")
async def test_twenty_five_ended_services_stop_the_run_and_the_owner_is_told_where_the_list_is(
    app: App,
) -> None:
    ali, group = await _stopped(app)
    assert panel_deletes(app) == []
    alerts = [t for t in await group.texts("errors") if "حذف خودکار متوقف شد" in t]
    assert len(alerts) == 1 and "25" in alerts[0], alerts
    assert "فهرست حذف" in alerts[0]
    batches = await _batches(app)
    assert len(batches) == 1 and batches[0].kind == "stop" and batches[0].status == "open"
    # The next daily run changes nothing: same list, no second alert.
    await run_lifecycle(app)
    await app.run_job("report_flush")
    assert len([t for t in await group.texts("errors") if "حذف خودکار متوقف شد" in t]) == 1
    assert len(await _batches(app)) == 1
    admin = app.admin(0)
    await open_lifecycle(admin)
    assert "🗂 فهرست حذف (۲۵)" in admin.buttons


@smoke("۱۶.۲۳")
async def test_the_list_shows_every_service_with_its_title_owner_and_reason_one_button_each(
    app: App,
) -> None:
    await _stopped(app)
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("🗂 فهرست حذف (۲۵)")
    seen: list[str] = []
    for _page in range(5):
        seen += _rows(admin)
        if "بعدی ➡️" not in admin.buttons:
            break
        await admin.tap("بعدی ➡️")
    assert len(seen) == N and len(set(seen)) == N, seen
    assert all("Ali" in r and "۱۵ روز از انقضا" in r for r in seen), seen[:2]
    assert any("خدمت ۰۰" in r.replace("00", "۰۰") or "خدمت 00" in r for r in seen)
    assert "⛔️ حذف همین فهرست (۲۵)" in admin.buttons
    # Callback data stays under Telegram's 64 bytes.
    for row in admin.last_message.markup.inline_keyboard:  # type: ignore[union-attr]
        for button in row:
            assert len((button.callback_data or "").encode()) <= 64


@smoke("۱۶.۲۴")
async def test_a_row_opens_the_existing_service_card_with_the_exemption_and_a_way_back(
    app: App,
) -> None:
    await _stopped(app)
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("🗂 فهرست حذف (۲۵)")
    target = next(b for b in _rows(admin) if "00" in b or "۰۰" in b)
    await admin.tap(target)
    assert "bk_00" in admin.last_text, admin.last_text  # the live card from the panel
    assert "📅 تمدید" in admin.buttons and "📦 افزودن حجم" in admin.buttons
    assert "🛡 حذف نشود" in admin.buttons and "⬅️ فهرست حذف" in admin.buttons
    await admin.tap("🛡 حذف نشود")
    assert "↩️ حذف شود" in admin.buttons and "معاف" in admin.last_text
    await admin.tap("⬅️ فهرست حذف")
    assert "⛔️ حذف همین فهرست (۲۵)" in admin.buttons


@smoke("۱۶.۲۴")
async def test_exempt_one_renew_one_approve_the_rest_and_the_skipped_are_named(app: App) -> None:
    ali, _group = await _stopped(app)
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("🗂 فهرست حذف (۲۵)")
    target = next(b for b in _rows(admin) if "00" in b or "۰۰" in b)
    await admin.tap(target)
    await admin.tap("🛡 حذف نشود")
    await admin.tap("⬅️ فهرست حذف")
    renewed = app.panel.user("bk_01")  # renewed while the list sat there
    renewed.status, renewed.expire = "active", datetime.now(UTC) + timedelta(days=30)

    await admin.tap("⛔️ حذف همین فهرست (۲۵)")
    assert "25" in admin.last_text  # the confirm screen repeats the count
    assert panel_deletes(app) == []
    await admin.tap("بله، حذف کن")
    deleted = panel_deletes(app)
    assert len(deleted) == 23 and "bk_00" not in deleted and "bk_01" not in deleted
    assert await is_deleted(app, "bk_05") and not await is_deleted(app, "bk_00")
    result = admin.last_text
    assert "23" in result and "معاف" in result and "فعال" in result
    assert "خدمت ۰۰" in result.replace("00", "۰۰") or "خدمت 00" in result
    assert admin.buttons, "the result carries a keyboard"

    # A second tap on the same confirm (double tap, stale button) deletes nothing more.
    count = len(panel_deletes(app))
    await admin.press(f"{LifeCB.APPROVE_OK}{(await _batches(app))[0].id}")
    assert len(panel_deletes(app)) == count
    assert "استفاده شده" in admin.last_text or "قبلاً" in admin.last_text
    assert (await _batches(app))[0].status == "used"


@smoke("۱۶.۲۵")
async def test_a_stale_list_is_void_and_offers_a_fresh_one(app: App) -> None:
    await _stopped(app)
    batch = (await _batches(app))[0]
    sqlite_exec(
        app,
        "UPDATE lifecycle_batches SET created_at = ? WHERE id = ?",
        (datetime.now(UTC) - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S.%f"),
        batch.id,
    )
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("🗂 فهرست حذف")
    assert "قدیمی" in admin.last_text or "باطل" in admin.last_text
    assert "🔄 ساخت فهرست تازه" in admin.buttons
    assert not any("⛔️ حذف همین فهرست" in b for b in admin.buttons)
    await admin.press(f"{LifeCB.APPROVE_OK}{batch.id}")  # a stale confirm button
    assert panel_deletes(app) == []
    await admin.press(f"{LifeCB.APPROVE}{batch.id}")
    assert panel_deletes(app) == []


@smoke("۱۶.۲۵")
async def test_a_list_made_under_other_numbers_is_void(app: App) -> None:
    await _stopped(app)
    batch = (await _batches(app))[0]
    await configure(app, delete_after_expiry_days=9)
    admin = app.admin(0)
    await admin.send("/admin")
    await admin.press(f"{LifeCB.APPROVE_OK}{batch.id}")
    assert panel_deletes(app) == []
    assert "باطل" in admin.last_text or "عدد" in admin.last_text


@smoke("۱۶.۲۵")
async def test_a_fresh_list_can_be_built_from_the_preview_and_approved(app: App) -> None:
    await _backlog(app, 8)
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("👁 پیش‌نمایش")
    await admin.tap("🔄 ساخت فهرست تازه")
    assert "⛔️ حذف همین فهرست (۸)" in admin.buttons
    await admin.tap("⛔️ حذف همین فهرست (۸)")
    await admin.tap("بله، حذف کن")
    assert len(panel_deletes(app)) == 8


@smoke("۱۶.۲۶")
async def test_another_admins_forged_approval_is_refused(app: App) -> None:
    await _stopped(app)
    batch = (await _batches(app))[0]
    helper = app.admin(1)
    await helper.send("/admin")
    await helper.press(f"{LifeCB.APPROVE}{batch.id}")
    assert helper.popup is not None
    await helper.press(f"{LifeCB.APPROVE_OK}{batch.id}")
    assert helper.popup is not None
    assert panel_deletes(app) == []
    assert (await _batches(app))[0].status == "open"


@smoke("۱۶.۲۶")
async def test_approval_is_refused_while_deleting_is_not_switched_on(app: App) -> None:
    await _stopped(app)
    batch = (await _batches(app))[0]
    await configure(app, delete_mode="rehearsal")
    admin = app.admin(0)
    await admin.send("/admin")
    await admin.press(f"{LifeCB.APPROVE_OK}{batch.id}")
    assert panel_deletes(app) == []


@smoke("۱۶.۲۷")
async def test_the_exemption_keeps_a_service_through_the_daily_job_too(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    for name in ("ex_a", "ex_b"):
        await seed_service(app, ali.user_id, name, status="expired", expires_in_days=-50)
    for i in range(6):
        await seed_service(app, ali.user_id, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    sqlite_exec(
        app, "UPDATE pasarguard_accounts SET delete_exempt = 1 WHERE panel_username = 'ex_a'"
    )
    await run_lifecycle(app)
    assert panel_deletes(app) == ["ex_b"]


@smoke("۱۶.۲۷")
async def test_an_approved_list_obeys_warn_first_a_service_not_warned_in_time_is_skipped_and_warned(
    app: App,
) -> None:
    group = await app.connect_report_group()
    ali = await _backlog(app)
    await configure(app, delete_mode="on", delete_warn_days_before=3)
    admin = app.admin(0)
    # Warn-first: the run deletes nothing. 25 warnings are more than twenty, so the warnings have
    # their own stop: none is sent, the owner is told once, and the list waits for him.
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    await app.run_job("report_flush")
    assert panel_deletes(app) == []
    assert [t for t in ali.texts_since(mark) if "حذف می‌شه" in t] == []  # type: ignore[attr-defined]
    assert len([t for t in await group.texts("errors") if "ارسال هشدار حذف هم متوقف شد" in t]) == 1
    warn_batch = (await _batches(app))[0]
    await admin.send("/admin")
    await admin.press(f"{LifeCB.APPROVE}{warn_batch.id}")
    assert "هشدار حذف فرستاده بشه" in admin.last_text
    await admin.press(f"{LifeCB.APPROVE_OK}{warn_batch.id}")
    assert panel_deletes(app) == []
    assert len([t for t in ali.texts_since(mark) if "حذف می‌شه" in t]) == N  # type: ignore[attr-defined]
    # Three days later all 25 qualify: more than twenty, so the run stops and stores a list.
    await age_delete_warnings(app, days=3)
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    batch = [b for b in await _batches(app) if b.status == "open"][0]
    # One of them lapsed again for a different expiry after its warning: a new period, so its old
    # warning does not count. Approval skips it and warns again (rule 3 applies to an approved list).
    other = app.panel.user("bk_02")
    other.expire = datetime.now(UTC) - timedelta(days=16)
    mark = ali.mark()  # type: ignore[attr-defined]
    await admin.send("/admin")
    await admin.press(f"{LifeCB.APPROVE}{batch.id}")
    await admin.press(f"{LifeCB.APPROVE_OK}{batch.id}")
    deleted = panel_deletes(app)
    assert len(deleted) == N - 1 and "bk_02" not in deleted
    assert not await is_deleted(app, "bk_02")
    again = [t for t in ali.texts_since(mark) if "حذف می‌شه" in t and "bk_02" in t]  # type: ignore[attr-defined]
    assert len(again) == 1, again
    assert "خدمت 02" in admin.last_text


async def _ended_and_live(app: App, ended: int, live: int = 80) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    for i in range(ended):
        await seed_service(app, ali.user_id, f"end_{i:02d}", status="expired", expires_in_days=-15)
    for i in range(live):
        await seed_service(app, ali.user_id, f"live_{i:02d}", status="active", expires_in_days=20)


@smoke("۱۶.۱۷")
async def test_twenty_one_deletions_in_one_run_stop_it_and_twenty_do_not(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.lifecycle_run.DEFAULT_PAUSE_SECONDS", 0)
    await _ended_and_live(app, 21)
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    await run_lifecycle(app)
    assert panel_deletes(app) == [], "21 is more than twenty: nothing is deleted"
    assert len(await _batches(app)) == 1


@smoke("۱۶.۱۷")
async def test_twenty_deletions_in_a_big_shop_go_through(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.lifecycle_run.DEFAULT_PAUSE_SECONDS", 0)
    await _ended_and_live(app, 20)
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    await run_lifecycle(app)
    assert len(panel_deletes(app)) == 20 and await _batches(app) == []


@smoke("۱۶.۱۷")
async def test_more_than_a_quarter_of_the_shop_stops_the_run_when_it_is_more_than_five(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.lifecycle_run.DEFAULT_PAUSE_SECONDS", 0)
    await _ended_and_live(app, 6, live=17)  # 6 of 23: more than five and more than 25%
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    await run_lifecycle(app)
    assert panel_deletes(app) == []


@smoke("۱۶.۱۷")
async def test_a_small_shop_can_delete_one_service(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.lifecycle_run.DEFAULT_PAUSE_SECONDS", 0)
    await _ended_and_live(app, 1, live=3)  # 1 of 4 = 25%, and only one
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    await run_lifecycle(app)
    assert len(panel_deletes(app)) == 1
