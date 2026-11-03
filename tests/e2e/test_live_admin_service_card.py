"""Live findings on the admin's service card."""

from __future__ import annotations

from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active, open_service
from tests.e2e.harness import App, publish_store


async def test_a_no_expiry_service_offers_no_extend_button(app: App) -> None:
    """Live: «📅 تمدید» was offered, and refused only after a number was typed."""
    store = await publish_store(app)
    ali = app.customer("ali", first_name="Ali")
    account = await buy_active(app, store, ali)
    admin = app.admin()

    await open_service(admin, "Ali")
    assert "📅 تمدید" in admin.buttons, "a service with an expiry can be extended"

    account.expire = None  # «بدون انقضا» on the panel
    await open_service(admin, "Ali")
    assert "📅 تمدید" not in admin.buttons, admin.buttons
    assert "📦 افزودن حجم" in admin.buttons


async def test_the_cut_off_confirm_is_a_danger_button_on_its_own_row(app: App) -> None:
    """Live: «⛔️ بله، قطع کن» was uncoloured and shared its row with «انصراف»."""
    store = await publish_store(app)
    ali = app.customer("ali", first_name="Ali")
    await buy_active(app, store, ali)
    admin = app.admin()
    await open_service(admin, "Ali")

    await tap_starting(admin, "⛔️ قطع سرویس")

    markup = admin.last_message.markup
    assert markup is not None
    (row,) = [r for r in markup.inline_keyboard if any("قطع کن" in b.text for b in r)]
    assert [b.text for b in row] == ["⛔️ بله، قطع کن"], admin.rows
    assert row[0].style == "danger"
    assert ["انصراف"] in admin.rows
