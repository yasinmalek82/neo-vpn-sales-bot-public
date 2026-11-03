"""Live: a second tap on the same admin button sent the screen again.

Twenty quick taps on «⚙️ تنظیمات» left five copies of the settings screen in
the chat. The second tap asks for the screen the message already shows,
Telegram answers «message is not modified», and the fallback meant for a
message that cannot be edited sent a new one.
"""

from __future__ import annotations

from bot.keyboards.admin import AdminCB
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App


async def test_a_second_tap_on_the_same_button_does_not_send_the_screen_again(app: App) -> None:
    admin = app.admin()
    await admin.send("/admin")
    home = admin.last_message
    await tap_starting(admin, "⚙️")
    settings = admin.last_text
    mark = admin.mark()

    await admin.press(AdminCB.SEC_SETTINGS, message=home)
    await admin.press(AdminCB.SEC_SETTINGS, message=home)

    assert admin.messages_since(mark) == [], "the repeated tap sent a duplicate screen"
    assert admin.last_text == settings
