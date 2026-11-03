"""Section ۱۶, rows ۱۶.۳۰ to ۱۶.۳۳: the cases an adversarial review found in the first version.

Each scenario here was red on 66ff2aa (the first version of the job). The rule they share:
every choice falls on the side of not deleting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from bot.keyboards.lifecycle import CODE_OF_FIELD, LifeCB
from database.models import OrderStatus
from database.models.lifecycle import NOTICE_DELETE_WARNING, ServiceNotice
from database.models.order import TRIAL_KIND_PERSONAL
from tests.e2e._s16_helpers import (
    account_state,
    age_delete_warnings,
    backdate_limited_since,
    configure,
    is_deleted,
    lifecycle_row,
    open_lifecycle,
    panel_deletes,
    run_lifecycle,
    seed_order,
    seed_service,
    sqlite_exec,
)
from tests.e2e.harness import App, smoke

ON = {"delete_mode": "on", "delete_warn_days_before": 0}


def _stamp(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%d %H:%M:%S.%f")


async def _shop(app: App, extra: int = 6):  # noqa: ANN202
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    for i in range(extra):
        await seed_service(app, ali.user_id, f"live_{i}", status="active", expires_in_days=20)
    return ali


# --- B1: the limited clock does not survive a change --------------------------------------------------------


@smoke("۱۶.۳۰")
async def test_b1_a_service_changed_after_its_limited_clock_began_starts_the_clock_again(
    app: App,
) -> None:
    ali = await _shop(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "lim_c", status="limited", gb=1, used_gb=1, expires_in_days=30)
    await configure(app, **ON)
    await run_lifecycle(app)
    await backdate_limited_since(app, "lim_c", days=20)
    # Renewed 5 days ago in the bot (its order was activated then); the panel is limited again.
    await seed_order(app, uid, "lim_c", status=OrderStatus.ACTIVE, age_days=5)
    await run_lifecycle(app)
    assert panel_deletes(app) == [], "a renewed service is not «limited for 20 days»"
    state = await account_state(app, "lim_c")
    assert state.limited_since is not None
    assert datetime.now(UTC) - state.limited_since.replace(tzinfo=UTC) < timedelta(hours=1)


@smoke("۱۶.۳۰")
async def test_b1_an_admin_extending_or_adding_volume_clears_the_clock_and_any_intent(
    app: App,
) -> None:
    from api_client.factory import get_pasarguard_client
    from services.service_ops import add_data, extend_days

    ali = await _shop(app)
    account_id = await seed_service(
        app, ali.user_id, "lim_d", status="limited", gb=1, used_gb=1, expires_in_days=30  # type: ignore[attr-defined]
    )
    for op in ("extend", "add"):
        await backdate_limited_since(app, "lim_d", days=20)
        sqlite_exec(
            app,
            "UPDATE pasarguard_accounts SET delete_requested_at = ? WHERE id = ?",
            _stamp(timedelta(hours=1)),
            account_id,
        )
        async with app.session() as session:
            client = await get_pasarguard_client(session, app.settings)
            try:
                if op == "extend":
                    await extend_days(session, client, account_id=account_id, days=30)
                else:
                    await add_data(session, client, account_id=account_id, gigabytes=1)
            finally:
                await client.aclose()
        state = await account_state(app, "lim_d")
        assert state.limited_since is None and state.delete_requested_at is None, op


# --- B2 -------------------------------------------------------------------------------------------------------------


@smoke("۱۶.۳۱")
async def test_b2_typing_a_deletion_number_while_deleting_is_on_goes_back_to_rehearsal_and_says_so(
    app: App,
) -> None:
    await configure(app, delete_mode="on", last_rehearsal_at=datetime.now(UTC))
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.press(f"{LifeCB.EDIT}{CODE_OF_FIELD['delete_after_expiry_days']}")
    await admin.send("2")
    assert "برگشت" in admin.last_text and "تمرین" in admin.last_text
    row = await lifecycle_row(app)
    assert row is not None and row.delete_mode == "rehearsal" and row.last_rehearsal_at is None


# --- B3: an old intent never finishes a live service -----------------------------------------------------------------


@smoke("۱۶.۳۲")
async def test_b3_an_intent_older_than_48_hours_is_cleared_never_finished(app: App) -> None:
    ali = await _shop(app)
    account_id = await seed_service(app, ali.user_id, "alive_i", status="active", expires_in_days=30)  # type: ignore[attr-defined]
    await configure(app, **ON)
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET delete_requested_at = ? WHERE id = ?",
        _stamp(timedelta(hours=49)),
        account_id,
    )
    del app.panel.users["alive_i"]  # one 404, as after a manual panel cleanup
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    state = await account_state(app, "alive_i")
    assert state.deleted_at is None and state.delete_requested_at is None
    assert ali.texts_since(mark) == []  # type: ignore[attr-defined]


@smoke("۱۶.۳۲")
async def test_r6_a_rehearsal_never_finishes_an_intent(app: App) -> None:
    ali = await _shop(app)
    account_id = await seed_service(app, ali.user_id, "gone_r", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, delete_mode="rehearsal", delete_warn_days_before=0)
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET delete_requested_at = ? WHERE id = ?",
        _stamp(timedelta(hours=1)),
        account_id,
    )
    del app.panel.users["gone_r"]
    await run_lifecycle(app)
    assert not await is_deleted(app, "gone_r")


@smoke("۱۶.۳۲")
async def test_r5_a_failed_provisioning_order_keeps_its_own_account_only(app: App) -> None:
    ali = await _shop(app, extra=0)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "pf_a", status="expired", expires_in_days=-50)
    await seed_order(app, uid, "pf_a", status=OrderStatus.PROVISION_FAILED)
    await seed_service(app, uid, "pf_b", status="expired", expires_in_days=-50)
    for i in range(6):
        await seed_service(app, uid, f"live_{i}", status="active", expires_in_days=20)
    await configure(app, **ON)
    await run_lifecycle(app)
    assert panel_deletes(app) == ["pf_b"]


# --- B4 / R7: what counts as a warning ----------------------------------------------------------------------------------


@smoke("۱۶.۳۳")
async def test_r7_a_crash_between_the_claim_and_the_send_is_not_a_warning(
    app: App, monkeypatch
) -> None:
    ali = await _shop(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_k", status="expired", expires_in_days=-60)
    await configure(app, delete_mode="on")

    class Crash(BaseException):
        pass

    real = app.bot.send_message

    async def dying(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise Crash

    monkeypatch.setattr(app.bot, "send_message", dying)
    with pytest.raises(Crash):
        await run_lifecycle(app)
    monkeypatch.setattr(app.bot, "send_message", real)
    # Claimed, never delivered. Three days later it still is no proof: warn again, do not delete.
    await age_delete_warnings(app, days=3)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert len([t for t in ali.texts_since(mark) if "حذف می‌شه" in t]) == 1  # type: ignore[attr-defined]


@smoke("۱۶.۳۳")
async def test_r7_a_bad_request_is_our_bug_not_a_warning_and_the_owner_is_told_once(
    app: App, monkeypatch
) -> None:
    group = await app.connect_report_group()
    ali = await _shop(app)
    await seed_service(app, ali.user_id, "svc_q", status="expired", expires_in_days=-60)  # type: ignore[attr-defined]
    await configure(app, delete_mode="on")
    real = app.bot.send_message

    async def bad(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities")  # type: ignore[arg-type]

    monkeypatch.setattr(app.bot, "send_message", bad)
    await run_lifecycle(app)
    await run_lifecycle(app)
    monkeypatch.setattr(app.bot, "send_message", real)
    await app.run_job("report_flush")
    async with app.session() as session:
        rows = (
            (
                await session.execute(
                    select(ServiceNotice).where(ServiceNotice.kind == NOTICE_DELETE_WARNING)
                )
            )
            .scalars()
            .all()
        )
    assert all(r.delivered_at is None and r.sent_at is None for r in rows), "released, not counted"
    alerts = [t for t in await group.texts("errors") if "رد شد" in t]
    assert len(alerts) == 1, alerts
    await age_delete_warnings(app, days=3)
    await run_lifecycle(app)
    assert panel_deletes(app) == []


@smoke("۱۶.۳۳")
async def test_r8_a_service_exempted_after_the_decision_is_not_warned(
    app: App, monkeypatch
) -> None:
    ali = await _shop(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    for name in ("w_1", "w_2", "w_3"):
        await seed_service(app, uid, name, status="expired", expires_in_days=-60)
    await configure(app, delete_mode="on")
    real = app.bot.send_message
    calls = {"n": 0}

    async def exempting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            sqlite_exec(
                app,
                "UPDATE pasarguard_accounts SET delete_exempt = 1 WHERE panel_username IN ('w_2','w_3')",
            )
        return await real(*args, **kwargs)

    monkeypatch.setattr(app.bot, "send_message", exempting)
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert len([t for t in ali.texts_since(mark) if "حذف می‌شه" in t]) == 1  # type: ignore[attr-defined]


@smoke("۱۶.۳۳")
async def test_r9_a_rehearsal_with_the_panel_down_does_not_unlock_on(app: App) -> None:
    group = await app.connect_report_group()
    ali = await _shop(app, extra=0)
    for i in range(4):
        await seed_service(app, ali.user_id, f"d_{i}", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, delete_mode="rehearsal")
    app.panel.go_down()
    await run_lifecycle(app)
    await app.run_job("report_flush")
    row = await lifecycle_row(app)
    assert row is not None and row.last_rehearsal_at is None
    assert any("شکست خورد" in t for t in await group.texts("system"))
    app.panel.go_up()
    await run_lifecycle(app)
    row = await lifecycle_row(app)
    assert row is not None and row.last_rehearsal_at is not None


@smoke("۱۶.۳۳")
async def test_r10_a_trial_an_admin_extended_follows_the_paid_rules(app: App) -> None:
    from database.models.service_operation import STATE_APPLIED, ServiceOperation

    ali = await _shop(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    account_id = await seed_service(app, uid, "trial_e", status="expired", expires_in_days=-5, gb=1)
    await seed_order(
        app,
        uid,
        "trial_e",
        status=OrderStatus.ACTIVE,
        trial_kind=TRIAL_KIND_PERSONAL,
        price_toman=0,
    )
    async with app.session() as session:
        session.add(
            ServiceOperation(
                account_id=account_id,
                kind="extend_days",
                target_expire_unix=1,
                requested_amount=3,
                state=STATE_APPLIED,
                operation_key="x",
                created_at=datetime.now(UTC) - timedelta(days=30),
                applied_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        await session.commit()
    await configure(app, delete_mode="on")  # warning days 3: a trial would be deleted at 2 days
    mark = ali.mark()  # type: ignore[attr-defined]
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    assert [
        t for t in ali.texts_since(mark) if "حذف شد" in t
    ] == []  # 5 days is not due under the paid number


@smoke("۱۶.۳۳")
async def test_r11_a_second_process_that_already_deleted_it_is_not_counted_or_announced(
    app: App, monkeypatch
) -> None:
    from api_client.client import PasarGuardClient

    ali = await _shop(app)
    await seed_service(app, ali.user_id, "twice", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **ON)
    original = PasarGuardClient.delete_user

    async def raced(self, username):  # noqa: ANN001, ANN202
        sqlite_exec(
            app,
            "UPDATE pasarguard_accounts SET deleted_at = ? WHERE panel_username = ?",
            _stamp(timedelta(0)),
            username,
        )
        await original(self, username)

    monkeypatch.setattr(PasarGuardClient, "delete_user", raced)
    mark = ali.mark()  # type: ignore[attr-defined]
    result = await run_lifecycle(app)
    assert result.deleted == 0
    assert [t for t in ali.texts_since(mark) if "حذف شد" in t] == []  # type: ignore[attr-defined]


@smoke("۱۶.۳۳")
async def test_r14_a_name_answering_as_another_panel_user_is_kept_and_reported(app: App) -> None:
    group = await app.connect_report_group()
    ali = await _shop(app)
    await seed_service(app, ali.user_id, "impostor", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    await configure(app, **ON)
    app.panel.user("impostor").id = 99_999  # same name, a different user on the panel
    await run_lifecycle(app)
    await app.run_job("report_flush")
    assert panel_deletes(app) == [] and not await is_deleted(app, "impostor")
    reports = [t for t in await group.texts("errors") if "شناسهٔ دیگه‌ای" in t]
    assert len(reports) == 1 and "impostor" in reports[0]


@smoke("۱۶.۳۳")
async def test_r1_a_change_between_the_intent_and_the_panel_call_aborts_and_clears_the_intent(
    app: App, monkeypatch
) -> None:
    from services import lifecycle_run

    ali = await _shop(app)
    uid = ali.user_id  # type: ignore[attr-defined]
    await seed_service(app, uid, "svc_a", status="expired", expires_in_days=-50)
    order_id = await seed_order(app, uid, "svc_a", status=OrderStatus.EXPIRED)
    await configure(app, **ON)
    real = lifecycle_run.load_facts
    calls = {"n": 0}

    async def spying(session, **kw):  # noqa: ANN001, ANN003, ANN202
        calls["n"] += 1
        if (
            calls["n"] == 4
        ):  # the look after the intent is committed (1 run, 2 before, 3 fresh, 4 again)
            sqlite_exec(app, "UPDATE orders SET status = 'PAYMENT_APPROVED' WHERE id = ?", order_id)
        return await real(session, **kw)

    monkeypatch.setattr(lifecycle_run, "load_facts", spying)
    await run_lifecycle(app)
    assert panel_deletes(app) == []
    state = await account_state(app, "svc_a")
    assert state.deleted_at is None and state.delete_requested_at is None


# --- R1 / R2 / R3: provisioning meets the job -------------------------------------------------------------------------------


async def _renew_and_approve(app: App, ali) -> object:  # noqa: ANN001
    from tests.e2e._s07b_helpers import approve_order, tap_starting
    from tests.e2e._s08_helpers import my_service

    await my_service(ali)
    await tap_starting(ali, "تمدید این سرویس")
    await tap_starting(ali, "🎫 بدون کد تخفیف")
    await tap_starting(ali, "ثبت سفارش")
    order = await app.latest_order(ali)
    await ali.send_photo()
    await approve_order(app, order.id)
    return await app.latest_order(ali)


@smoke("۱۶.۳۴")
async def test_r1_provisioning_meets_a_fresh_intent_by_retrying_never_by_refusing(app: App) -> None:
    from tests.e2e._s08_helpers import buy_active
    from tests.e2e.harness import publish_store

    store = await publish_store(app)
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    account_id = (await account_state(app, app.panel.only_user().username)).id
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET delete_requested_at = ? WHERE id = ?",
        _stamp(timedelta(minutes=1)),
        account_id,
    )
    puts = len(app.panel.requests_to("PUT /api/user/{username}"))
    order = await _renew_and_approve(app, ali)
    assert order.status == OrderStatus.PROVISIONING, "the order stays paid and is retried"
    assert len(app.panel.requests_to("PUT /api/user/{username}")) == puts, "no panel call yet"


@smoke("۱۶.۳۴")
async def test_r1_an_old_intent_is_cleared_by_the_renewal_before_its_panel_call(app: App) -> None:
    from tests.e2e._s08_helpers import buy_active
    from tests.e2e.harness import publish_store

    store = await publish_store(app)
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    name = app.panel.only_user().username
    account_id = (await account_state(app, name)).id
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET delete_requested_at = ?, limited_since = ? WHERE id = ?",
        _stamp(timedelta(minutes=30)),
        _stamp(timedelta(days=20)),
        account_id,
    )
    order = await _renew_and_approve(app, ali)
    assert order.status == OrderStatus.ACTIVE
    state = await account_state(app, name)
    assert state.delete_requested_at is None and state.limited_since is None


def _force_name(monkeypatch, *names: str) -> None:  # noqa: ANN001
    from services import provisioning

    forced = iter(names)
    real = provisioning.build_panel_username
    monkeypatch.setattr(
        provisioning,
        "build_panel_username",
        lambda service_name: next(forced, None) or real(service_name),
    )


async def _delete_in_place(app: App, name: str) -> None:
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET deleted_at = ? WHERE panel_username = ?",
        _stamp(timedelta(days=3)),
        name,
    )
    app.panel.users.pop(name, None)


@smoke("۱۶.۳۴")
async def test_r2_another_customers_deleted_name_is_taken_and_a_new_name_is_rolled(
    app: App, monkeypatch
) -> None:
    from tests.e2e._s08_helpers import buy_active
    from tests.e2e.harness import publish_store

    store = await publish_store(app)
    alice = app.customer("alice")
    await alice.start()
    bob = app.customer("bob")
    await bob.start()
    await seed_service(app, alice.user_id, "taken_nm", status="expired", expires_in_days=-50)
    await _delete_in_place(app, "taken_nm")
    _force_name(monkeypatch, "taken_nm")
    created = await buy_active(app, store, bob, name="bobsvc")
    assert created.username != "taken_nm"
    old = await account_state(app, "taken_nm")
    assert old.deleted_at is not None, "Alice's row was not revived"
    async with app.session() as session:
        from database.models import User

        owner = await session.get(User, old.user_id)
    assert owner is not None and owner.telegram_id == alice.user_id


@smoke("۱۶.۳۴")
async def test_r2_the_same_owner_re_creating_the_name_revives_the_row(
    app: App, monkeypatch
) -> None:
    from tests.e2e._s08_helpers import buy_active
    from tests.e2e.harness import publish_store

    store = await publish_store(app)
    alice = app.customer("alice")
    await alice.start()
    await seed_service(app, alice.user_id, "mine_nm", status="expired", expires_in_days=-50)
    sqlite_exec(
        app,
        "UPDATE pasarguard_accounts SET delete_requested_at = ?, delete_exempt = 1 WHERE panel_username = 'mine_nm'",
        _stamp(timedelta(days=3)),
    )
    await _delete_in_place(app, "mine_nm")
    _force_name(monkeypatch, "mine_nm")
    created = await buy_active(app, store, alice, name="again")
    assert created.username == "mine_nm"
    state = await account_state(app, "mine_nm")
    assert state.deleted_at is None and state.delete_requested_at is None
    assert state.delete_exempt is True, "the same owner keeps his own exemption"


@smoke("۱۶.۳۴")
async def test_r3_a_second_deletion_after_a_revive_is_announced_again(app: App) -> None:
    from sqlalchemy import select as _select

    from services.lifecycle_run import _soft_delete

    ali = await _shop(app, extra=0)
    account_id = await seed_service(app, ali.user_id, "again_nm", status="expired", expires_in_days=-50)  # type: ignore[attr-defined]
    for moment in (datetime.now(UTC) - timedelta(days=9), datetime.now(UTC)):
        async with app.session() as session:
            assert await _soft_delete(session, account_id, moment)
            await session.commit()
        sqlite_exec(
            app, "UPDATE pasarguard_accounts SET deleted_at = NULL WHERE id = ?", account_id
        )
    async with app.session() as session:
        rows = (
            (await session.execute(_select(ServiceNotice).where(ServiceNotice.kind == "deleted")))
            .scalars()
            .all()
        )
    assert len(rows) == 2
