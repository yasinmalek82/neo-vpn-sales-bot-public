"""Section ۱۶, rows ۱۶.۱۰ to ۱۶.۲۴: the daily job that deletes ended services.

«A mistake here is expensive for me.» Every scenario that deletes also proves
what stays; the ones that keep are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from database.models import Order, OrderStatus
from database.models.order import TRIAL_KIND_PERSONAL
from tests.e2e._s16_helpers import (
    account_state,
    age_delete_warnings,
    backdate_limited_since,
    before_nth_read,
    configure,
    is_deleted,
    money_rows,
    panel_deletes,
    run_lifecycle,
    seed_order,
    seed_service,
    sqlite_exec,
)
from tests.e2e.harness import GB, App, smoke

DELETE_NOW = {"delete_mode": "on", "delete_warn_days_before": 0}


def _notice(texts_seen: list[str]) -> list[str]:
    return [t for t in texts_seen if "حذف شد" in t]


def _warning(texts_seen: list[str]) -> list[str]:
    return [t for t in texts_seen if "حذف می‌شه" in t]


async def _customer(app: App, name: str = "ali") -> object:
    who = app.customer(name, first_name=name.title())
    await who.start()
    return who


# --- off, zero, rehearsal ----------------------------------------------------------------------


@smoke("۱۶.۱۰")
async def test_mode_off_deletes_nothing_and_does_not_even_read_the_panel(app: App) -> None:
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "old_a", status="expired", expires_in_days=-100)  # type: ignore[attr-defined]
    await run_lifecycle(app)
    await configure(app, delete_mode="off")
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert app.panel.requests_to("GET /api/user/{username}") == []
    assert not await is_deleted(app, "old_a")


@smoke("۱۶.۱۰")
async def test_a_number_of_zero_deletes_nothing_of_that_kind(app: App) -> None:
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "old_a", status="expired", expires_in_days=-100)  # type: ignore[attr-defined]
    await seed_service(app, ali.user_id, "lim_a", status="limited", gb=1, used_gb=1)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW, delete_after_expiry_days=0, delete_after_limited_days=0)
    await run_lifecycle(app)
    await backdate_limited_since(app, "lim_a", days=100)
    await run_lifecycle(app)
    assert panel_deletes(app) == []


@smoke("۱۶.۱۱")
async def test_a_rehearsal_deletes_nothing_tells_no_customer_and_reports_the_list(
    app: App,
) -> None:
    group = await app.connect_report_group()
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "old_a", status="expired", expires_in_days=-15, label="سرویس قدیمی")  # type: ignore[attr-defined]
    await seed_service(app, ali.user_id, "fresh_a", status="active", expires_in_days=20)  # type: ignore[attr-defined]
    await configure(app, delete_mode="rehearsal", delete_warn_days_before=0)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    await app.run_job("report_flush")
    assert panel_deletes(app) == []
    assert not await is_deleted(app, "old_a")
    assert ali.texts_since(mark) == []  # type: ignore[attr-defined]
    report = [t for t in await group.texts("system") if "تمرین" in t]
    assert len(report) == 1, report
    assert "سرویس قدیمی" in report[0] and "15 روز از انقضا" in report[0]
    assert "fresh_a" not in report[0]
    from tests.e2e._s16_helpers import lifecycle_row

    row = await lifecycle_row(app)
    assert row is not None and row.last_rehearsal_at is not None
    # A second run the same day does not post a second report.
    await run_lifecycle(app)
    await app.run_job("report_flush")
    assert len([t for t in await group.texts("system") if "تمرین" in t]) == 1


@smoke("۱۶.۱۱")
async def test_after_a_rehearsal_the_owner_can_turn_deleting_on(app: App) -> None:
    from tests.e2e._s16_helpers import lifecycle_row, open_lifecycle

    await app.connect_report_group()
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "old_a", status="expired", expires_in_days=-15)  # type: ignore[attr-defined]
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("روشن")
    assert "رد شد" in admin.last_text
    await open_lifecycle(admin)
    await admin.tap("👁 تمرین")
    await run_lifecycle(app)
    await open_lifecycle(admin)
    await admin.tap("روشن")
    assert "بله، روشن کن" in " ".join(admin.buttons)
    await admin.tap("بله، روشن کن")
    row = await lifecycle_row(app)
    assert row is not None and row.delete_mode == "on"


# --- the clocks ------------------------------------------------------------------------------------


@smoke("۱۶.۱۲")
async def test_expired_eleven_days_is_deleted_ten_and_nine_are_not_and_one_notice_goes(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "eleven", status="expired", expires_in_days=-11.01)
    await seed_service(app, uid, "ten", status="expired", expires_in_days=-10)
    await seed_service(app, uid, "nine", status="expired", expires_in_days=-9)
    for i in range(6):  # a shop with other services: one deletion is a small share
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    before = await money_rows(app)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == ["eleven"]
    assert "eleven" not in app.panel.users and {"ten", "nine"} <= set(app.panel.users)
    assert await is_deleted(app, "eleven")
    assert not await is_deleted(app, "ten") and not await is_deleted(app, "nine")
    notices = _notice(ali.texts_since(mark))  # type: ignore[attr-defined]
    assert len(notices) == 1 and "eleven" in notices[0], notices
    assert await money_rows(app) == before
    # A second run deletes nothing more and sends no second notice.
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == ["eleven"]
    assert ali.texts_since(mark) == []  # type: ignore[attr-defined]


@smoke("۱۶.۱۳")
async def test_a_service_limited_eleven_days_ago_is_deleted_and_never_in_the_run_that_first_sees_it(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "lim_a", status="limited", gb=1, used_gb=1, expires_in_days=30)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    await run_lifecycle(app)  # first sight: the clock starts, nothing is deleted
    assert panel_deletes(app) == []
    state = await account_state(app, "lim_a")
    assert state.limited_since is not None
    await run_lifecycle(app)  # same day: still nothing
    assert panel_deletes(app) == []
    await backdate_limited_since(app, "lim_a", days=11)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["lim_a"]


@smoke("۱۶.۱۳")
async def test_a_user_who_got_volume_back_is_kept_and_its_clock_is_cleared(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "lim_b", status="limited", gb=1, used_gb=1, expires_in_days=30)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    await run_lifecycle(app)
    await backdate_limited_since(app, "lim_b", days=11)
    panel_user = app.panel.user("lim_b")
    panel_user.status, panel_user.data_limit = "active", 5 * GB  # volume added
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert (await account_state(app, "lim_b")).limited_since is None
    # Limited again: the clock starts over, so it is not deleted now.
    panel_user.status, panel_user.used_traffic = "limited", 5 * GB
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert (await account_state(app, "lim_b")).limited_since is not None


@smoke("۱۶.۱۴")
async def test_a_trial_is_deleted_a_day_after_it_ends_and_a_paid_service_is_not(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "trial_a", status="expired", expires_in_days=-2.1, gb=1)
    await seed_order(
        app,
        uid,
        "trial_a",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
    )
    await seed_service(app, uid, "trial_b", status="expired", expires_in_days=-1, gb=1)
    await seed_order(
        app,
        uid,
        "trial_b",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
    )
    await seed_service(app, uid, "paid_a", status="expired", expires_in_days=-5)
    await seed_order(app, uid, "paid_a", status=OrderStatus.ACTIVE)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == ["trial_a"]
    assert _warning(ali.texts_since(mark)) == []  # type: ignore[attr-defined]


# --- always kept ------------------------------------------------------------------------------------


@smoke("۱۶.۱۵")
async def test_active_on_hold_disabled_and_no_expiry_services_are_never_deleted(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(
        app, uid, "act", status="active", expires_in_days=-100
    )  # the panel has not flipped it
    await seed_service(app, uid, "hold", status="on_hold", expires_in_days=-100)
    await seed_service(app, uid, "off", status="disabled", expires_in_days=-100)
    await seed_service(app, uid, "noexp", status="expired", expires_in_days=None)
    await seed_service(app, uid, "recent", status="expired", expires_in_days=-100, age_days=0)
    await configure(app, **DELETE_NOW)
    await run_lifecycle(app)
    assert panel_deletes(app) == []


@smoke("۱۶.۱۵")
async def test_an_order_awaiting_payment_review_or_provisioning_keeps_the_customers_services(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "old_a", status="expired", expires_in_days=-100)
    await seed_order(app, uid, "old_a", status=OrderStatus.ACTIVE)
    await seed_order(app, uid, "old_a", status=OrderStatus.PAYMENT_REVIEW, age_days=0)
    await configure(app, **DELETE_NOW)
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    sqlite_exec(
        app, "UPDATE orders SET status = 'PAYMENT_APPROVED' WHERE status = 'PAYMENT_REVIEW'"
    )
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    sqlite_exec(app, "UPDATE orders SET status = 'PROVISIONING' WHERE status = 'PAYMENT_APPROVED'")
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    # Once nothing is pending (and the change is a day old) it is an ordinary ended service.
    sqlite_exec(app, "UPDATE orders SET status = 'REJECTED' WHERE status = 'PROVISIONING'")
    await app.age(Order, 2, days=3, columns=("created_at", "updated_at", "paid_at", "activated_at"))
    await run_lifecycle(app)
    assert panel_deletes(app) == ["old_a"]


@smoke("۱۶.۱۵")
async def test_a_panel_user_the_bot_has_no_account_for_is_never_deleted(app: App) -> None:
    from tests.e2e._s16_helpers import panel_user

    ali = await _customer(app)
    await seed_service(app, ali.user_id, "mine", status="expired", expires_in_days=-100)  # type: ignore[attr-defined]
    panel_user(app, "orphan", status="expired", expires_in_days=-300)
    await configure(app, **DELETE_NOW)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["mine"]
    assert "orphan" in app.panel.users


# --- the re-read before the delete ----------------------------------------------------------------------


@smoke("۱۶.۱۶")
async def test_a_renewal_between_the_decision_and_the_delete_saves_the_service(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_r", status="expired", expires_in_days=-50)
    await configure(app, **DELETE_NOW)
    panel_user = app.panel.user("svc_r")

    def renew() -> None:
        panel_user.status = "active"
        panel_user.expire = datetime.now(UTC) + timedelta(days=30)

    before_nth_read(app, "svc_r", 2, renew)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert "svc_r" in app.panel.users and not await is_deleted(app, "svc_r")
    assert ali.texts_since(mark) == []  # type: ignore[attr-defined]
    assert (await account_state(app, "svc_r")).delete_requested_at is None


@smoke("۱۶.۱۶")
async def test_an_order_that_reaches_review_between_the_two_reads_saves_the_service(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_o", status="expired", expires_in_days=-50)
    order_id = await seed_order(app, uid, "svc_o", status=OrderStatus.EXPIRED)
    await configure(app, **DELETE_NOW)
    before_nth_read(
        app,
        "svc_o",
        2,
        lambda: sqlite_exec(
            app, "UPDATE orders SET status = 'PAYMENT_REVIEW' WHERE id = ?", order_id
        ),
    )
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert not await is_deleted(app, "svc_o")


@smoke("۱۶.۱۸")
async def test_a_panel_timeout_on_the_reread_deletes_nothing_and_changes_nothing(app: App) -> None:
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "svc_t", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW)
    before_nth_read(app, "svc_t", 2, lambda: app.panel.timeout_next("GET /api/user/{username}"))
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    state = await account_state(app, "svc_t")
    assert state.deleted_at is None and state.delete_requested_at is None
    assert ali.texts_since(mark) == []  # type: ignore[attr-defined]
    # The next run, with a healthy panel, deletes it.
    await run_lifecycle(app)
    assert panel_deletes(app) == ["svc_t"]


@smoke("۱۶.۱۸")
async def test_a_panel_error_on_the_first_read_keeps_the_service(app: App) -> None:
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "svc_e", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW)
    app.panel.fail_next("GET /api/user/{username}", status=500)
    await run_lifecycle(app)
    assert panel_deletes(app) == [] and not await is_deleted(app, "svc_e")


@smoke("۱۶.۱۸")
async def test_a_panel_that_answers_not_found_for_everyone_deletes_and_marks_nothing(
    app: App,
) -> None:
    """A wrong panel address: every read is a 404. That must never look like «already gone»."""
    ali = await _customer(app)
    for name in ("a1", "a2", "a3"):
        await seed_service(app, ali.user_id, name, status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW)
    for user in list(app.panel.users):
        del app.panel.users[user]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    for name in ("a1", "a2", "a3"):
        state = await account_state(app, name)
        assert state.deleted_at is None and state.delete_requested_at is None


@smoke("۱۶.۱۸")
async def test_a_404_on_the_delete_itself_counts_as_already_gone(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_g", status="expired", expires_in_days=-50)
    await seed_service(app, uid, "other", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    app.panel.fail_next("DELETE /api/user/{username}", status=404)  # someone deleted it meanwhile
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert await is_deleted(app, "svc_g")
    assert len(_notice(ali.texts_since(mark))) == 1  # type: ignore[attr-defined]


@smoke("۱۶.۱۸")
async def test_a_404_on_the_reread_without_an_intent_only_keeps_the_service(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_h", status="expired", expires_in_days=-50)
    await seed_service(app, uid, "other", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    before_nth_read(app, "svc_h", 2, lambda: app.panel.users.pop("svc_h", None))
    await run_lifecycle(app)
    state = await account_state(app, "svc_h")
    assert state.deleted_at is None and state.delete_requested_at is None


# --- the two phases: a crash between them converges ------------------------------------------------------------


@smoke("۱۶.۱۹")
async def test_the_intent_is_committed_before_the_panel_is_asked_to_delete(
    app: App, monkeypatch
) -> None:
    import sqlite3

    from api_client.client import PasarGuardClient

    ali = await _customer(app)
    await seed_service(app, ali.user_id, "svc_i", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW)
    seen: list[object] = []
    original = PasarGuardClient.delete_user

    async def checking(self: PasarGuardClient, username: str) -> None:
        conn = sqlite3.connect(app.db_path)  # another connection sees only what is committed
        try:
            seen.append(
                conn.execute(
                    "SELECT delete_requested_at FROM pasarguard_accounts WHERE panel_username = ?",
                    (username,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        await original(self, username)

    monkeypatch.setattr(PasarGuardClient, "delete_user", checking)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["svc_i"]
    assert seen and seen[0] is not None, "the panel was called before the intent was committed"


@smoke("۱۶.۱۹")
async def test_a_crash_between_the_panel_delete_and_our_commit_converges_on_the_next_run(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_c", status="expired", expires_in_days=-50)
    await seed_service(app, uid, "other", status="active", expires_in_days=20)
    await configure(app, **DELETE_NOW)
    app.panel.timeout_next("DELETE /api/user/{username}", applied=True)  # applied, reply lost
    await run_lifecycle(app)
    assert "svc_c" not in app.panel.users, "the panel did delete it"
    state = await account_state(app, "svc_c")
    assert state.deleted_at is None and state.delete_requested_at is not None
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    state = await account_state(app, "svc_c")
    assert state.deleted_at is not None
    assert len(_notice(ali.texts_since(mark))) == 1  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert len(_notice(ali.texts_since(mark))) == 1  # type: ignore[attr-defined]


@smoke("۱۶.۱۹")
async def test_an_intent_whose_user_is_alive_and_no_longer_qualifies_is_cleared(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_k", status="expired", expires_in_days=-50)
    await configure(app, **DELETE_NOW)
    app.panel.fail_next("DELETE /api/user/{username}", status=500)
    await run_lifecycle(app)
    assert (await account_state(app, "svc_k")).delete_requested_at is not None
    user = app.panel.user("svc_k")
    user.status, user.expire = "active", datetime.now(UTC) + timedelta(days=30)  # renewed
    await run_lifecycle(app)
    state = await account_state(app, "svc_k")
    assert state.delete_requested_at is None and state.deleted_at is None
    assert panel_deletes(app) == []


@smoke("۱۶.۱۹")
async def test_an_intent_with_a_404_is_not_finished_when_the_panel_knows_none_of_our_users(
    app: App,
) -> None:
    """Evidence the panel is the right one: at least one other service must answer."""
    ali = await _customer(app)
    await seed_service(app, ali.user_id, "svc_w", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **DELETE_NOW)
    app.panel.fail_next("DELETE /api/user/{username}", status=500)
    await run_lifecycle(app)
    del app.panel.users["svc_w"]  # now every read is a 404, and no other user answers
    await run_lifecycle(app)
    assert (await account_state(app, "svc_w")).deleted_at is None


# --- warn first, then delete ----------------------------------------------------------------------------------------


async def _three_old_services(app: App) -> tuple[object, list[str]]:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    names = ["old_1", "old_2", "old_3"]
    for name in names:
        await seed_service(app, uid, name, status="expired", expires_in_days=-60)
        await seed_order(app, uid, name, status=OrderStatus.ACTIVE)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    return ali, names


@smoke("۱۶.۲۰")
async def test_the_first_real_run_over_old_services_only_warns_and_three_days_later_it_deletes(
    app: App,
) -> None:
    ali, names = await _three_old_services(app)
    await configure(app, delete_mode="on")  # delete_warn_days_before stays 3
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    warnings = _warning(ali.texts_since(mark))  # type: ignore[attr-defined]
    assert len(warnings) == 3, warnings
    assert any("تمدید" in b for m in ali.messages_since(mark) for b in m.buttons)  # type: ignore[attr-defined]
    await run_lifecycle(app)  # the same day: no second warning, still no deletion
    assert panel_deletes(app) == []
    assert len(_warning(ali.texts_since(mark))) == 3  # type: ignore[attr-defined]

    assert await age_delete_warnings(app, days=3) == 3
    await run_lifecycle(app)
    assert sorted(panel_deletes(app)) == names
    notices = _notice(ali.texts_since(mark))  # type: ignore[attr-defined]
    assert len(notices) == 3
    assert len(_warning(ali.texts_since(mark))) == 3  # type: ignore[attr-defined]


@smoke("۱۶.۲۰")
async def test_a_warning_less_than_the_warning_days_old_does_not_allow_the_delete(app: App) -> None:
    ali, _names = await _three_old_services(app)
    await configure(app, delete_mode="on")
    await run_lifecycle(app)
    await age_delete_warnings(app, days=2)
    await run_lifecycle(app)
    assert panel_deletes(app) == []


@smoke("۱۶.۲۰")
async def test_a_renewal_after_the_warning_saves_the_service_and_a_later_lapse_needs_a_new_warning(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_n", status="expired", expires_in_days=-60)
    await seed_order(app, uid, "svc_n", status=OrderStatus.ACTIVE)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on")
    await run_lifecycle(app)
    await age_delete_warnings(app, days=3)
    user = app.panel.user("svc_n")
    user.status, user.expire = "active", datetime.now(UTC) + timedelta(days=30)  # renewed
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    # It lapses again, long ago, for the new expiry: the old warning is for the old period.
    user.status, user.expire = "expired", datetime.now(UTC) - timedelta(days=61)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert len(_warning(ali.texts_since(mark))) == 1  # type: ignore[attr-defined]
    await age_delete_warnings(app, days=3)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["svc_n"]


@smoke("۱۶.۲۰")
async def test_a_trial_is_never_warned_and_a_warning_number_of_zero_turns_warning_off(
    app: App,
) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "trial_w", status="expired", expires_in_days=-5, gb=1)
    await seed_order(
        app,
        uid,
        "trial_w",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
    )
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on")  # warning days 3: still no warning for a trial
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == ["trial_w"]
    assert _warning(ali.texts_since(mark)) == []  # type: ignore[attr-defined]


@smoke("۱۶.۲۰")
async def test_a_service_inside_the_warning_window_is_warned_before_its_deadline(app: App) -> None:
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_w", status="expired", expires_in_days=-8.5)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on")
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    got = _warning(ali.texts_since(mark))  # type: ignore[attr-defined]
    assert len(got) == 1 and "svc_w" in got[0]
    await run_lifecycle(app)
    assert len(_warning(ali.texts_since(mark))) == 1  # type: ignore[attr-defined]
    assert panel_deletes(app) == []


# --- lists, ledger -----------------------------------------------------------------------------------------------------


@smoke("۱۶.۲۱")
async def test_a_deleted_service_leaves_the_customers_resellers_and_admins_lists_and_nothing_else_changes(
    app: App,
) -> None:
    from bot.keyboards.reply_menu import ACCOUNT
    from tests.e2e._s07b_helpers import open_card

    shop = app.customer("shop", first_name="Shop")
    await shop.start()
    await app.give_wallet(shop, 50_000)
    uid = shop.user_id
    await seed_service(
        app,
        uid,
        "gone_one",
        status="expired",
        expires_in_days=-50,
        reseller=True,
        label="رضا | 0912",
    )
    await seed_order(app, uid, "gone_one", status=OrderStatus.ACTIVE)
    await seed_service(
        app, uid, "kept_one", status="active", expires_in_days=20, label="سارا | 0935"
    )
    for i in range(5):
        await seed_service(
            app, uid, f"live_{i}", status="active", expires_in_days=20, label=f"c{i}"
        )
    await shop.tap_reply(ACCOUNT)
    assert any("رضا" in b for b in shop.buttons)
    await configure(app, **DELETE_NOW)
    before = await money_rows(app)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["gone_one"]
    assert await money_rows(app) == before
    await shop.tap_reply(ACCOUNT)
    assert not any("رضا" in b for b in shop.buttons) and any("سارا" in b for b in shop.buttons)
    admin = app.admin(0)
    await open_card(admin, "Shop")
    await admin.tap("📱 سرویس")
    assert not any("gone_one" in b for b in admin.buttons)
    assert await app.wallet_balance(shop) == 50_000


@smoke("۱۶.۲۲")
async def test_the_preview_lists_the_services_the_next_run_deletes_and_deletes_nothing(
    app: App,
) -> None:
    from tests.e2e._s16_helpers import open_lifecycle

    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "p_old1", status="expired", expires_in_days=-30, label="قدیمی یک")
    await seed_service(app, uid, "p_old2", status="expired", expires_in_days=-40, label="قدیمی دو")
    await seed_service(app, uid, "p_young", status="expired", expires_in_days=-3, label="جوان")
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on", delete_warn_days_before=0)
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("👁 پیش‌نمایش")
    rows = [b for b in admin.buttons if "قدیمی" in b or "جوان" in b]
    assert len(rows) == 2 and not any("جوان" in r for r in rows), rows
    assert all("روز از انقضا" in r for r in rows)
    assert not any("⛔️ حذف همین فهرست" in b for b in admin.buttons), "a preview cannot be approved"
    assert panel_deletes(app) == []
    await run_lifecycle(app)
    assert sorted(panel_deletes(app)) == ["p_old1", "p_old2"]


@smoke("۱۶.۲۰")
async def test_a_customer_who_blocked_the_bot_is_not_warned_again_and_the_owner_is_told_it_never_arrived(
    app: App,
) -> None:
    group = await app.connect_report_group()
    ali = await _customer(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_b", status="expired", expires_in_days=-60)
    await seed_order(app, uid, "svc_b", status=OrderStatus.ACTIVE)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, delete_mode="on")
    app.tg.block(uid)
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    sends = [c for c in app.tg.calls if c.method == "SendMessage"]
    before = len(sends)
    await run_lifecycle(app)  # not retried every day: the claim stays
    assert len([c for c in app.tg.calls if c.method == "SendMessage"]) == before
    assert await age_delete_warnings(app, days=3) == 1
    await run_lifecycle(app)
    assert panel_deletes(app) == [
        "svc_b"
    ], "a warning the customer cannot receive does not block it"
    await app.run_job("report_flush")
    report = [t for t in await group.texts("system") if "حذف خودکار" in t]
    assert len(report) == 1 and "هشدار نرسید: مشتری ربات را بسته" in report[0], report
