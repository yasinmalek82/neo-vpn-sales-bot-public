"""Section ۱۷, rows ۱۷.۱ to ۱۷.۳: the owner's screen for marketing nudges."""

from __future__ import annotations

from bot.keyboards.marketing import CODE_OF_CAMPAIGN, MktCB
from bot.texts import texts
from database.models.marketing import CAMPAIGNS
from tests.e2e._s17_helpers import marketing_row, open_marketing
from tests.e2e.harness import App, smoke

OFF = [
    "❌ 🛒 سفارش نیمه‌کاره",
    "❌ 🎁 بعد از تست",
    "❌ 👋 بعد از پایان اشتراک",
    "❌ ⚠️ حجم زودتر از زمان",
]


@smoke("۱۷.۱")
async def test_every_campaign_ships_off_and_looking_writes_nothing(app: App) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    assert all(button in admin.buttons for button in OFF), admin.buttons
    assert admin.last_text.count("خاموش") >= len(CAMPAIGNS)
    assert "روشن از" not in admin.last_text
    assert (await marketing_row(app)) is None, "looking at the screen writes nothing"


@smoke("۱۷.۲")
async def test_a_toggle_flips_one_campaign_and_stores_the_moment(app: App) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    await admin.press(f"{MktCB.TOGGLE}{CODE_OF_CAMPAIGN['winback']}")
    assert texts.admin_mkt_saved in admin.last_text
    assert "✅ 👋 بعد از پایان اشتراک" in admin.buttons
    assert "❌ 🛒 سفارش نیمه‌کاره" in admin.buttons
    assert "روشن از" in admin.last_text
    first = await marketing_row(app)
    assert first is not None and first.winback_enabled_at is not None
    assert first.cart_enabled_at is None and first.upgrade_enabled_at is None

    await admin.press(f"{MktCB.TOGGLE}{CODE_OF_CAMPAIGN['winback']}")
    assert "❌ 👋 بعد از پایان اشتراک" in admin.buttons
    off = await marketing_row(app)
    assert off is not None and off.winback_enabled_at is None


@smoke("۱۷.۳")
async def test_a_helper_admin_sees_the_screen_but_cannot_switch_anything(app: App) -> None:
    helper = app.admin(1)
    await open_marketing(helper)
    assert "پیام‌های بازاریابی" in helper.last_text
    assert not any(b.startswith(("✅", "❌")) and "🛒" in b for b in helper.buttons)
    # A forged tap is refused as well.
    await helper.press(f"{MktCB.TOGGLE}{CODE_OF_CAMPAIGN['cart']}")
    assert helper.popup == texts.admin_mkt_not_owner
    assert (await marketing_row(app)) is None
