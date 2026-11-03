"""Helpers shared by package E1 (sections ۲ to ۶): disruption reports, trials, purchases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from bot.keyboards.reply_menu import SUPPORT
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import Actor, App
from tests.e2e.harness.app import running_app


async def report_disruption(customer: Actor, isp: str, city: str | None) -> None:
    """«💬 پشتیبانی» → «📡 گزارش اختلال» → the operator → a city button (or ``None``: type later)."""
    await customer.tap_reply(SUPPORT)
    await tap_starting(customer, "📡 گزارش اختلال")
    await customer.tap(isp)
    if city is not None:
        await customer.tap(city)


async def app_with(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> AsyncIterator[App]:
    """An ``App`` whose settings differ from the harness default (``support_handle=""``)."""
    async with running_app(migrated_template, tmp_path, monkeypatch, **overrides) as running:
        yield running


async def open_admin_panel(admin: Actor, label: str) -> None:
    """/admin and then one top-level button."""
    await admin.send("/admin")
    await tap_starting(admin, label)


async def set_group_trial(app: App, store: Any, key: str, gb: int, days: int) -> None:
    """Give one store group a trial (or ``gb=0``: take it away), as the group dashboard does."""
    from services.store_admin import commit_and_activate, set_category_trial

    async with app.session() as session:
        await set_category_trial(
            session, store.category_ids[key], data_limit_gb=gb, duration_days=days
        )
        await commit_and_activate(session, notes="تست رایگان تغییر کرد")


async def open_group_trial(admin: Actor, parent: str, group: str) -> None:
    """/admin → فروشگاه → (parent →) group → «🎟 تست»."""
    await admin.send("/admin")
    await tap_starting(admin, "🛍 فروشگاه")
    if parent:
        await tap_starting(admin, parent)
    await tap_starting(admin, group)
    await tap_starting(admin, "🎟 تست")


async def card_order_from_summary(customer: Actor, name: str = "ali12") -> Any:
    """From the service-name question to a card order: name, no promo code, «ثبت سفارش»."""
    await customer.send(name)
    await customer.tap("بدون کد تخفیف")
    await customer.tap("ثبت سفارش")
    return await customer.app.latest_order(customer)


async def open_shelf(customer: Actor, *path: str) -> None:
    """«خرید اشتراک» and then each group title in turn."""
    from bot.keyboards.reply_menu import BUY

    await customer.tap_reply(BUY)
    for title in path:
        await tap_starting(customer, title)


async def pay_and_approve(app: App, customer: Actor, order: Any) -> None:
    """The receipt photo, then the owner's «✅ تایید»."""
    from tests.e2e._s07b_helpers import approve_order

    await customer.send_photo()
    await approve_order(app, int(order.id))


async def open_volume_wizard(admin: Actor, *, other: bool = True) -> None:
    """/admin → فروشگاه → حجمی → ویژه → «➕ پلن جدید» (→ «✏️ مقدار دیگر» at the volume step)."""
    await admin.send("/admin")
    await tap_starting(admin, "🛍 فروشگاه")
    await tap_starting(admin, "حجمی")
    await tap_starting(admin, "ویژه")
    await tap_starting(admin, "➕ پلن جدید")
    if other:
        await tap_starting(admin, "✏️ مقدار دیگر")
