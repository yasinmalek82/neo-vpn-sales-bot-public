"""Live finding: the owner's sale report named the product by an internal row.

«محصول: volume-10GB-30d-p70000-h0-g6-8» in the «فروش» topic, for a ready plan
bought from the store. The report names it the way the store does.
"""

from __future__ import annotations

import pytest

from database.models import Plan
from tests.e2e._s01_helpers import open_shelf, pay_and_approve
from tests.e2e._s07b_helpers import add_custom_volume, approve_order, tap_starting
from tests.e2e._s08_helpers import buy_active, my_service
from tests.e2e.harness import App, Store, publish_store
from tests.e2e.harness.expectations import report_plain


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await add_custom_volume(app, s)
    return s


async def _sale_reports(app: App, group) -> list[str]:  # noqa: ANN001
    await app.run_job("report_flush")
    return [t for t in await group.texts("sales") if "کد سفارش" in t]


def _product_line(report: str) -> str:
    (line,) = [ln for ln in report.splitlines() if "محصول" in ln]
    return line


async def _internal_plan_names(app: App) -> list[str]:
    from sqlalchemy import select

    return [str(name) for name in await app.all(select(Plan.name))]


async def test_a_ready_plan_sale_is_named_like_the_shelf(app: App, store: Store) -> None:
    group = await app.connect_report_group()
    ali = app.customer("ali")
    await buy_active(app, store, ali, "special_50")

    (sale,) = await _sale_reports(app, group)

    plan = store.plans["special_50"]
    line = _product_line(sale)
    assert store.groups[plan.group].title in line, line
    assert report_plain(plan.title) in line, line
    for internal in await _internal_plan_names(app):
        assert internal not in sale, sale


async def test_a_renewal_report_is_named_like_the_shelf(app: App, store: Store) -> None:
    group = await app.connect_report_group()
    ali = app.customer("ali")
    await buy_active(app, store, ali, "economy_50")
    await my_service(ali)
    await tap_starting(ali, "تمدید این سرویس")
    await tap_starting(ali, "بدون کد")
    await tap_starting(ali, "ثبت سفارش")
    order = await app.latest_order(ali)
    await ali.send_photo()
    await approve_order(app, order.id)

    _, renewal = await _sale_reports(app, group)

    assert "تمدید" in renewal
    plan = store.plans["economy_50"]
    line = _product_line(renewal)
    assert store.groups[plan.group].title in line, line
    assert report_plain(plan.title) in line, line
    for internal in await _internal_plan_names(app):
        assert internal not in renewal, renewal


async def test_a_custom_purchase_is_described_by_volume_and_days(app: App, store: Store) -> None:
    group = await app.connect_report_group()
    ali = app.customer("ali")
    await ali.start()
    await open_shelf(ali, "حجمی", "ویژه")
    await tap_starting(ali, "🧩 خرید سفارشی")
    await tap_starting(ali, "۵۰ گیگ")
    await tap_starting(ali, "۱ ماه")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)
    await pay_and_approve(app, ali, order)

    (sale,) = await _sale_reports(app, group)

    line = _product_line(sale)
    assert "50 گیگ" in line and "30 روز" in line, line
    for internal in await _internal_plan_names(app):
        assert internal not in sale, sale
