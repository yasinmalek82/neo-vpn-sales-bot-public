"""Shared stretches of the customer's path, so a scenario starts where its row does.

Each helper only taps and types what a person would; it asserts nothing about the
screens it passes through (the scenarios do that). It supports the **ready volume
plans** of ``publish_store``; other shapes (unlimited, custom) have their own
questions, so a scenario for those taps them itself.
"""

from __future__ import annotations

from typing import Any

from bot.keyboards.reply_menu import BUY
from services.ux_format import to_persian_digits
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.store import PlanInfo, Store


def _group_path(store: Store, plan: PlanInfo) -> list[str]:
    group = store.groups[plan.group]
    path = [group.title]
    while group.parent:
        group = store.groups[group.parent]
        path.insert(0, group.title)
    return path


async def browse_to_plan(
    customer: Actor, store: Store, plan_key: str = "special_50", *, via_keyboard: bool = True
) -> None:
    """«خرید اشتراک» → the group(s) → the plan, ending on the service-name question.

    ``via_keyboard=False`` types the label instead of tapping it, for a person whose
    bottom keyboard never arrived.
    """
    plan = store.plans[plan_key]
    if via_keyboard:
        await customer.tap_reply(BUY)
    else:
        await customer.send(BUY)
    for title in _group_path(store, plan):
        await customer.tap(title)
    try:
        await customer.tap(plan.title)
    except AssertionError:
        # The shelf opens on the first duration tab; a longer plan sits on its own.
        months = to_persian_digits((plan.duration_days or 30) // 30)
        await customer.tap(f"{months} ماهه")
        await customer.tap(plan.title)


async def order_by_card(
    customer: Actor,
    store: Store,
    plan_key: str = "special_50",
    name: str = "ali12",
    *,
    via_keyboard: bool = True,
) -> Any:
    """From the shelf to the card instructions. Returns the new order row.

    Types ``name`` as the service name, skips the promo code, confirms the summary.
    """
    await browse_to_plan(customer, store, plan_key, via_keyboard=via_keyboard)
    await customer.send(name)
    await customer.tap("بدون کد تخفیف")
    await customer.tap("ثبت سفارش")
    return await customer.app.latest_order(customer)


async def pay_by_card(customer: Actor, store: Store, plan_key: str = "special_50") -> Any:
    """``order_by_card`` and then the receipt photo. Returns the order (now in review)."""
    order = await order_by_card(customer, store, plan_key)
    await customer.send_photo()
    return order
