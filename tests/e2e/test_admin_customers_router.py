"""The separated customer-browsing router must stay wired and admin-only."""

from __future__ import annotations

from bot.keyboards.admin import AdminCB
from bot.texts import texts
from tests.e2e.harness import App


async def test_customer_browsing_is_routed_once_and_refuses_forged_callbacks(app: App) -> None:
    routers = app.dp.sub_routers
    customer_router = next((router for router in routers if router.name == "admin_customers"), None)
    assert customer_router is not None, "the customer-browsing router must be registered"
    assert routers.index(customer_router) < next(
        i for i, router in enumerate(routers) if router.name == "fallback"
    )

    # Each entry point belongs to the new router exactly once, not an unreachable
    # duplicate left behind in the old admin router.
    expected = {
        "admin_users_list",
        "admin_users_page",
        "admin_users_filter",
        "admin_customer_start",
        "admin_customer_search",
        "admin_customer_pick",
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
    assert all(name == "admin_customers" for name, _ in registered)

    customer = app.customer("ali")
    await customer.start()
    for data in (
        AdminCB.CUSTOMER,
        f"{AdminCB.CUSTOMER_PAGE}all:0",
        f"{AdminCB.CUSTOMER_FILTER}all",
        AdminCB.CUSTOMER_SEARCH,
        f"{AdminCB.CUSTOMER_PICK}1",
    ):
        mark = customer.mark()
        await customer.press(data)
        assert customer.popup == texts.admin_action_denied
        assert customer.texts_since(mark) == []
