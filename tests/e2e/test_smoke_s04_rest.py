"""Section 4, the rows ``test_smoke_s04_card_purchase.py`` leaves.

Row ۴.۱۲ (connect the link in a real client and load a site) is MANUAL: it needs a real VPN
client and a panel node that carries traffic. What the bot controls is asserted in ۴.۱۰
and ۴.۱۱: the link in the card is the panel's subscription URL for the created account.
The checklist ends at ۴.۱۴; ۴.۱۵ to ۴.۱۷ are the ۴.۳الف, ۴.۳ب and ۴.۳ج rows of the other file.

Two extra guards on the delivery that row ۴.۱۰ promises: it happens once even if the owner
taps «✅ تایید» twice, and the QR is on the brand template (``.claude/rules/qr.md``).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from database.models import OrderStatus
from tests.e2e._s01_helpers import pay_and_approve
from tests.e2e.harness import App, Store, order_by_card, publish_store, smoke

TEMPLATE = Path(__file__).resolve().parents[2] / "assets" / "brand" / "qr_template_default.png"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _approve_twice(app: App, store: Store):
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("✅ تایید")
    mark = ali.mark()
    await admin.tap("✅ تایید")  # the old button on the receipt message is still there
    return ali, mark


@smoke("۴.۱۰")
async def test_a_second_tap_on_approve_builds_and_charges_nothing_twice(
    app: App, store: Store
) -> None:
    ali, _ = await _approve_twice(app, store)
    assert len(app.panel.users) == 1
    assert len(app.panel.requests_to("POST /api/user")) == 1
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE
    assert await app.wallet_balance(ali) == 0


@smoke("۴.۱۰")
async def test_a_second_tap_on_approve_does_not_send_the_customer_a_second_card(
    app: App, store: Store
) -> None:
    ali, mark = await _approve_twice(app, store)
    assert ali.messages_since(mark) == [], "the customer got a second card"


@smoke("۴.۱۰")
async def test_a_repeated_retry_tap_does_not_send_the_customer_a_second_card(
    app: App, store: Store
) -> None:
    """The panel fails once on the owner's approval; «تلاش مجدد» then works, and tapped again does not repeat."""
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    await ali.send_photo()
    app.panel.fail_next("POST /api/user", status=500)
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("✅ تایید")
    assert (await app.latest_order(ali)).status == OrderStatus.PROVISION_FAILED
    await admin.tap("📋 جزئیات سفارش")
    detail = admin.last_message
    retry = next(
        b.callback_data
        for r in detail.markup.inline_keyboard
        for b in r
        if b.text.startswith("تلاش مجدد")
    )
    mark = ali.mark()
    await admin.press(retry, message=detail)
    assert [m.kind for m in ali.messages_since(mark)] == ["photo"], "the first retry sends the card"
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE
    mark = ali.mark()
    await admin.press(retry, message=detail)  # the same button, tapped again
    assert ali.messages_since(mark) == [], "the customer got a second card"
    assert len(app.panel.users) == 1


@smoke("۴.۱۰")
async def test_the_qr_is_the_brand_card_and_the_link_matches_the_panel(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store)
    mark = ali.mark()
    await pay_and_approve(app, ali, order)
    (card,) = [m for m in ali.messages_since(mark) if m.kind == "photo"]
    account = app.panel.only_user()
    assert f"{app.panel.base_url}/sub/{account.token}" in card.text
    image = Image.open(io.BytesIO(card.file_bytes or b""))
    assert image.size == Image.open(TEMPLATE).size, "the QR is not on the brand template"
