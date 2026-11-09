"""The admin wallet router keeps money changes and access checks intact."""

from __future__ import annotations

from bot.keyboards.admin import AdminCB
from bot.texts import texts
from tests.e2e._s07_helpers import button_cb, db_user, ledger, open_customer_card
from tests.e2e.harness import App


async def test_wallet_routes_are_registered_once_and_reject_forged_callbacks(app: App) -> None:
    routers = app.dp.sub_routers
    wallet_router = next((router for router in routers if router.name == "admin_wallet"), None)
    assert wallet_router is not None, "the dedicated wallet router must be registered"
    assert routers.index(wallet_router) < next(
        i for i, router in enumerate(routers) if router.name == "fallback"
    )
    expected = {
        "admin_wallet_start",
        "admin_wallet_amount",
        "admin_wallet_apply",
        "admin_wallet_zero_ask",
        "admin_wallet_zero_apply",
        "admin_late_receipt_credit",
    }
    registered = [
        (router.name, handler.callback)
        for router in routers
        for observer in (router.callback_query, router.message)
        for handler in observer.handlers
        if handler.callback.__name__ in expected
    ]
    assert {handler.__name__ for _, handler in registered} == expected
    assert len(registered) == len(expected)
    assert all(name == "admin_wallet" for name, _ in registered)

    customer = app.customer("ali")
    await customer.start()
    user_id = (await db_user(app, customer)).id
    for data in (
        f"{AdminCB.WALLET_ADD}{user_id}",
        f"{AdminCB.WALLET_OK}{user_id}",
        f"{AdminCB.WALLET_ZERO}{user_id}",
        f"{AdminCB.WALLET_ZERO_OK}{user_id}",
        f"{AdminCB.LATE_CREDIT}1",
    ):
        await customer.press(data)
        assert customer.popup == texts.admin_action_denied
    assert await app.wallet_balance(customer) == 0
    assert await ledger(app, customer) == []


async def test_a_replayed_wallet_confirmation_changes_the_balance_once(app: App) -> None:
    customer = app.customer("ali")
    await customer.start()
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("🏦 کیف پول")
    await admin.send("50000")
    confirm_screen = admin.last_message
    confirm_data = button_cb(confirm_screen, "تأیید و اعمال")
    mark = customer.mark()
    await admin.press(confirm_data, message=confirm_screen)
    await admin.press(confirm_data, message=confirm_screen)
    assert await app.wallet_balance(customer) == 50_000
    entries = await ledger(app, customer)
    assert [(entry.entry_type.value, entry.amount_toman) for entry in entries] == [
        ("credit", 50_000)
    ]
    assert len(customer.texts_since(mark)) == 1
