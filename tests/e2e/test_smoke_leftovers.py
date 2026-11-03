"""Rows elsewhere in the checklist that the other packages left open.

Row ۲.۳ is automated here.
MANUAL: ۳.۳ and ۴.۱۲ (open the subscription link in a real client and test the internet: only a real
VPN client and a real panel node can answer; the fake panel proves the account carries the right
panel group, see ۹.۱۲ and ۴.۳).
STALE: ۷ب.۸ (the per-plan «↩️» button is gone; already reported by the reseller scenarios).
"""

from __future__ import annotations

from aiogram.types import ReplyKeyboardRemove

from tests.e2e._s09_helpers import bottom_keyboard
from tests.e2e.harness import App, smoke

MENU = [
    "🛒 خرید اشتراک",
    "🎟 تست رایگان",
    "📡 سرویس‌های من",
    "🏦 کیف پول",
    "🎁 دعوت دوستان",
    "💬 پشتیبانی",
]


@smoke("۲.۳")
async def test_the_bottom_keyboard_stays_put_however_many_messages_pile_up(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    assert ali.reply_buttons == MENU
    for i in range(12):
        await ali.send(f"پیام شمارهٔ {i}")
    for label in ("💬 پشتیبانی", "🎁 دعوت دوستان", "🏦 کیف پول", "📡 سرویس‌های من"):
        await ali.tap_reply(label)
    assert ali.reply_buttons == MENU, "the menu left the screen"
    assert len(ali.messages) >= 15, "the chat did scroll"
    keyboard = bottom_keyboard(app)
    assert keyboard.is_persistent and keyboard.resize_keyboard
    removed = [
        c for c in app.tg.calls if isinstance(c.params.get("reply_markup"), ReplyKeyboardRemove)
    ]
    assert removed == [], "the bot removed the bottom keyboard"
    await ali.tap_reply("🛒 خرید اشتراک")  # and it still works after all that
    assert "✏️" not in ali.last_text
