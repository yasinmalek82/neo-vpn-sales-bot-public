"""Section ۱۱: inviting friends.

Rows ۱۱.۱ to ۱۱.۱۰ and ۱۱.۳ب.
STALE: ۱۱.۱۰ says the referrer's reward «هنوز کار نمی‌کند»; it does now: the referrer is told the
reward was recorded as pending and the stats count it, while nothing reaches the wallet.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from database.models import ReferralLog, User
from database.models.referral_log import ReferralStatus
from tests.e2e._s01_helpers import app_with
from tests.e2e._s07b_helpers import approve_order
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s09_helpers import invite_code, open_with_code
from tests.e2e.harness import (
    App,
    Store,
    browse_to_plan,
    digits,
    publish_store,
    smoke,
)
from tests.e2e.harness.settings import BOT_USERNAME

INVITE_BUTTON = "🎁 دعوت دوستان"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


def _link(code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={code}"


async def _pitch(customer) -> str:  # type: ignore[no-untyped-def]
    mark = customer.mark()
    await customer.tap_reply(INVITE_BUTTON)
    return customer.messages_since(mark)[-1].text


@smoke("۱۱.۱")
async def test_the_invite_button_sends_two_messages_stats_then_a_forwardable_pitch(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    mark = ali.mark()
    await ali.tap_reply(INVITE_BUTTON)
    first, second = ali.messages_since(mark)
    assert len(ali.messages_since(mark)) == 2
    code = await invite_code(ali)
    assert "کد تو:" in first.text and code in first.text and "دعوت‌شده‌ها: 0" in first.text
    assert first.buttons, "the stats message carries the buttons"
    assert "این متن رو برای دوستات بفرست" in second.text or "برای دوستات" in second.text
    # Its one button is a link to Telegram's share sheet: nothing that needs this chat.
    (share,) = [b for row in second.markup.inline_keyboard for b in row]
    assert share.url and share.callback_data is None, share


@smoke("۱۱.۲")
async def test_the_pitch_is_an_ad_then_the_link_last_then_the_first_purchase_discount(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    pitch = await _pitch(ali)
    body = pitch.split("\n\n", 1)[1]
    assert body.rstrip().endswith(_link(code)), "the link is the last thing"
    assert "خرید اولت 5٪ تخفیف داره" in body, "the first-purchase discount is explained"
    before_link = body.rsplit(_link(code), 1)[0]
    assert before_link.index("نئو نتورک") < before_link.index("تخفیف"), "ad first, discount after"
    # Every number in it is one the owner configured: the cheapest plan, the hours, the percent.
    numbers = {
        int(n) for n in re.findall(r"\d+", digits(before_link).replace(",", "").replace("٬", ""))
    }
    assert numbers == {90_000, 9, 24, 5}, numbers


@pytest.fixture
async def bare_app(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[App]:
    async for running in app_with(
        migrated_template,
        tmp_path,
        monkeypatch,
        support_handle="",
        support_hours_fa="",
        channel_handle="",
        referral_referred_discount_percent=0,
    ):
        yield running


@smoke("۱۱.۲")
async def test_a_number_the_owner_never_set_is_dropped_with_its_whole_claim(
    bare_app: App,
) -> None:
    ali = bare_app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    pitch = await _pitch(ali)
    text = pitch.replace(_link(code), "")
    assert not re.search(r"\d", digits(text)), f"an unset number reached the customer:\n{pitch}"
    assert "تومان" not in text and "تخفیف" not in text and "@" not in text
    assert pitch.rstrip().endswith(_link(code))
    bare_app.final_checks()


@smoke("۱۱.۳")
async def test_the_forwardable_text_carries_no_stats_or_reward(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    friend = app.customer("friend")
    await friend.start()
    await open_with_code(friend, await invite_code(ali))  # ali now has a referred friend
    pitch = await _pitch(ali)
    for leaked in ("دعوت‌شده‌ها", "پاداش", "کد تو", "تسویه", "کیف پول"):
        assert leaked not in pitch, f"{leaked!r} would be forwarded to the friend"
    assert not re.search(r"[۱-۹]\s*نفر", pitch)


@smoke("۱۱.۳ب")
async def test_the_last_line_is_the_referrers_own_link_and_never_the_signature(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    pitch = await _pitch(ali)
    lines = [ln for ln in pitch.splitlines() if ln.strip()]
    assert lines[-1].strip() == _link(code)
    assert "─────" not in pitch and "نئو نتورک · @" not in pitch
    assert pitch.count(f"@{BOT_USERNAME}") == 0, "a bare @handle is a way in without the code"


@smoke("۱۱.۴")
async def test_a_brand_new_account_opening_the_link_gets_the_code_registered(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    new = app.customer("new")
    said = await open_with_code(new, code)
    assert any("کد دعوت ثبت شد" in t for t in said), said
    row = await app.one(select(User).where(User.telegram_id == new.user_id))
    referrer = await app.one(select(User).where(User.telegram_id == ali.user_id))
    assert row.referred_by_id == referrer.id


@smoke("۱۱.۵")
async def test_someone_who_started_before_but_never_bought_is_registered_too(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    old = app.customer("old")
    await old.start()  # pressed /start earlier: the case that used to be ignored in silence
    said = await open_with_code(old, code)
    assert any("کد دعوت ثبت شد" in t for t in said), said
    row = await app.one(select(User).where(User.telegram_id == old.user_id))
    referrer = await app.one(select(User).where(User.telegram_id == ali.user_id))
    assert row.referred_by_id == referrer.id


@smoke("۱۱.۶")
async def test_someone_who_already_bought_is_refused_with_a_clear_message(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    code = await invite_code(ali)
    buyer = app.customer("buyer")
    await buy_active(app, store, buyer)
    said = await open_with_code(buyer, code)
    assert any("چون قبلاً از ربات خرید کردی" in t and "ثبت نمی‌شه" in t for t in said), said
    row = await app.one(select(User).where(User.telegram_id == buyer.user_id))
    assert row.referred_by_id is None


@smoke("۱۱.۷")
async def test_a_second_referrer_does_not_replace_the_first(app: App) -> None:
    ali, sara = app.customer("ali"), app.customer("sara")
    await ali.start()
    await sara.start()
    first, second = await invite_code(ali), await invite_code(sara)
    new = app.customer("new")
    await open_with_code(new, first)
    said = await open_with_code(new, second)
    assert any("قبلاً با دعوت یه نفر دیگه اومدی" in t for t in said), said
    row = await app.one(select(User).where(User.telegram_id == new.user_id))
    first_referrer = await app.one(select(User).where(User.telegram_id == ali.user_id))
    assert row.referred_by_id == first_referrer.id


@smoke("۱۱.۸")
async def test_opening_your_own_link_is_refused(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    said = await open_with_code(ali, await invite_code(ali))
    assert any("نمی‌تونی خودت رو دعوت کنی" in t for t in said), said
    row = await app.one(select(User).where(User.telegram_id == ali.user_id))
    assert row.referred_by_id is None


async def _invited_first_purchase(app: App, store: Store):  # type: ignore[no-untyped-def]
    ali, new = app.customer("ali"), app.customer("new")
    await ali.start()
    await open_with_code(new, await invite_code(ali))
    await browse_to_plan(new, store, "special_50")
    await new.send("n12")
    await new.tap("بدون کد تخفیف")
    summary = new.last_text
    await new.tap("ثبت سفارش")
    return ali, new, summary, await app.latest_order(new)


@smoke("۱۱.۹")
async def test_the_invited_friends_first_purchase_gets_the_discount(app: App, store: Store) -> None:
    _ali, new, summary, order = await _invited_first_purchase(app, store)
    price = store.plans["special_50"].price
    assert order.discount_toman == price * 5 // 100 == 7_500
    assert order.price_toman == price - 7_500
    assert "142500" in digits(summary).replace("٬", "")
    # Paid and active: a second purchase is no longer a «first purchase».
    await new.send_photo()
    await approve_order(app, order.id)
    await browse_to_plan(new, store, "economy_50")
    await new.send("n13")
    if "✨ سرویس جدید" in new.buttons:
        await new.tap("✨ سرویس جدید")
    await new.tap("بدون کد تخفیف")
    assert "تخفیف: 0 تومان" in digits(new.last_text), new.last_text
    assert "قیمت اولیه: 90000" in digits(new.last_text)
    assert "مبلغ نهایی سفارش: 90000" in digits(new.last_text)


@smoke("۱۱.۱۰")
async def test_the_referrers_reward_is_pending_and_nothing_reaches_the_wallet(
    app: App, store: Store
) -> None:
    ali, new, _summary, order = await _invited_first_purchase(app, store)
    await new.send_photo()
    await approve_order(app, order.id)
    assert await app.wallet_balance(ali) == 0, "the reward must not be paid out yet"
    log = await app.one(select(ReferralLog))
    assert log.status is ReferralStatus.pending
    assert any("پاداش تو" in m.text and "در انتظار تسویه" in m.text for m in ali.messages)
    await ali.tap_reply(INVITE_BUTTON)
    stats = next(m.text for m in reversed(ali.messages) if "کد تو:" in m.text)
    assert "دعوت‌شده‌ها: 1" in stats and "پاداش در انتظار: 1" in stats
    assert "پاداش تسویه‌شده: 0" in stats
