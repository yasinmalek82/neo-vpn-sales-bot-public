"""Section 2: a customer meets the bot for the first time.

Rows covered: ۲.۱ ۲.۴ ۲.۵ ۲.۶. The other rows of the section need Telegram's own
menu button (۲.۲), a long scroll (۲.۳), a group chat (۲.۱۰) or tomorrow's digest
(۲.۱۴); see the report for which are MANUAL.
"""

from __future__ import annotations

import pytest

from bot.keyboards.reply_menu import ACCOUNT, BUY, REFERRAL, SUPPORT, TRIAL, WALLET
from bot.texts import texts
from tests.e2e.harness import App, browse_to_plan, plain, publish_store, smoke

THE_FIVE = [BUY, ACCOUNT, WALLET, REFERRAL, SUPPORT]


@smoke("۲.۱")
async def test_start_is_one_message_with_the_five_bottom_buttons(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    assert len(ali.messages) == 1, f"/start must send one message, not two:\n{ali.transcript()}"
    for label in THE_FIVE:
        assert label in ali.reply_buttons, f"{label!r} missing from {ali.reply_buttons}"


@smoke("۲.۱")
async def test_start_keyboard_also_offers_the_free_trial(app: App) -> None:
    """The checklist counts five buttons; the bot has since added «تست رایگان» as a sixth."""
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    assert TRIAL in ali.reply_buttons


# Every bottom button opens its own screen, and every message on it has a keyboard.
_SCREENS = [
    pytest.param(BUY, id="buy"),
    pytest.param(TRIAL, id="trial"),
    pytest.param(ACCOUNT, id="services"),
    pytest.param(WALLET, id="wallet"),
    pytest.param(REFERRAL, id="referral"),
    pytest.param(SUPPORT, id="support"),
]


@smoke("۲.۴")
@pytest.mark.parametrize("label", _SCREENS)
async def test_each_bottom_button_opens_a_screen_where_every_message_has_buttons(
    app: App, label: str
) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    mark = ali.mark()
    await ali.tap_reply(label)
    arrived = ali.messages_since(mark)
    assert arrived, f"tapping {label!r} answered nothing"
    bare = [m for m in arrived if not m.buttons]
    assert not bare, f"messages with no button after {label!r}: {bare}"


@smoke("۲.۵")
async def test_irrelevant_text_gets_guidance_not_silence(app: App) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    mark = ali.mark()
    await ali.send("سلام کسی هست")
    assert ali.texts_since(mark) == [plain(texts.unknown_message)]
    assert BUY in ali.reply_buttons, "the bottom keyboard must survive an unknown message"


@smoke("۲.۶")
async def test_legacy_keyboard_text_is_understood_as_buy(app: App) -> None:
    """A migrated customer still has the previous bot's «خرید سرویس 🛍» button on screen."""
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    mark = ali.mark()
    await ali.send("خرید سرویس 🛍")
    assert ali.texts_since(mark)[0] == plain(texts.purchase_choose_type)
    assert plain(texts.unknown_message) not in ali.texts_since(mark)


# ۲.۶, the other side of the keyword guess: it must not eat what a customer types inside a flow.


@smoke("۲.۶")
async def test_a_service_name_that_mentions_a_menu_word_stays_in_the_purchase(app: App) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    question = ali.last_text
    await ali.send("سرویس من")  # not a valid name, but it is an answer to the name question
    assert ali.last_text != question and "ثبت سفارش" not in " ".join(ali.buttons)
    await ali.send("ali12")
    assert "بدون کد تخفیف" in " ".join(ali.buttons), "the purchase did not go on"


@smoke("۲.۶")
async def test_a_promo_code_that_mentions_a_menu_word_is_read_as_a_code(app: App) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.send("تست")
    assert "این کد تخفیف معتبر نیست" in ali.last_text, ali.last_text


@smoke("۲.۶")
async def test_a_bottom_button_tapped_inside_a_flow_still_navigates(app: App) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, "special_50")
    await ali.tap_reply(WALLET)
    assert "موجودی" in ali.last_text, ali.last_text
    await ali.send("ali12")  # the name question is gone with the flow
    assert "بدون کد تخفیف" not in " ".join(ali.buttons)


@smoke("۲.۴")
async def test_the_invite_text_carries_a_share_button_with_the_same_link(app: App) -> None:
    from urllib.parse import parse_qs, urlsplit

    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await ali.tap_reply(REFERRAL)
    pitch = ali.last_message
    assert pitch.text.startswith(plain(texts.referral_pitch_intro)), "still forwardable as it was"
    (button,) = [b for row in pitch.markup.inline_keyboard for b in row]
    assert button.text == "📤 فرستادن برای دوستان"
    url = urlsplit(button.url)
    assert (url.scheme, url.netloc, url.path) == ("https", "t.me", "/share/url")
    shared = parse_qs(url.query)
    (link,) = shared["url"]
    assert (
        "?start=" in link and link in pitch.text
    ), "the button shares another link than the screen"
    assert shared["text"][0].strip(), "the share sheet opens with no invite text"
    assert " " not in button.url and "\n" not in button.url, "the link and text must be URL-encoded"
