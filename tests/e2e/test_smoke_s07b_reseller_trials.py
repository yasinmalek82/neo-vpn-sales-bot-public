"""Section ۷ب, part 3: a reseller gives trials to their customers.

Rows: ۷ب.۱۲ to ۷ب.۲۱. The monthly quota is 2, an extra trial costs 5000, the wallet
holds 8000: two free, one charged (3000 left), and a fourth that cannot be paid.

The checklist numbers the trials loosely («۱۴: again, ۰ left» comes after two have
been made). What is asserted is the sequence the quota implies: free with 1 left,
free with 0 left, charged 5000, refused.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from bot.texts import texts
from database.models import User
from tests.e2e._s07b_helpers import (
    enable_reseller_trial,
    make_shop,
    open_card,
    take_trial,
    tap_starting,
)
from tests.e2e.harness import App, Store, digits, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

QUOTA, EXTRA, WALLET = 2, 5000, 8000


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await enable_reseller_trial(app, s, "unlimited", "special")
    return s


async def _wallet(app: App, telegram_id: int) -> int:
    user = await app.one(select(User).where(User.telegram_id == telegram_id))
    return int(user.wallet_balance_toman)


@smoke("۷ب.۱۲", "۷ب.۱۳", "۷ب.۱۳الف", "۷ب.۱۳ب", "۷ب.۱۳ج", "۷ب.۱۴", "۷ب.۱۵", "۷ب.۱۶", "۷ب.۱۷")
async def test_two_free_trials_then_a_charged_one_then_a_refusal(app: App, store: Store) -> None:
    shop, _ = await make_shop(app, trial=(QUOTA, EXTRA), wallet=WALLET)
    assert await _wallet(app, shop.user_id) == WALLET

    # ۷ب.۱۳: the customer is asked first, and the remaining quota is stated.
    await shop.tap_reply("🎟 تست رایگان")
    assert shop.last_text == report_plain(texts.reseller_trial_pick_group)
    await tap_starting(shop, "ویژه")
    assert shop.last_text == report_plain(texts.reseller_label_name_prompt) + "\n\n" + report_plain(
        texts.reseller_trial_label_free.format(remaining=str(2))
    )
    # ۷ب.۱۳الف: the name alone, then the phone is asked on its own with a skip button.
    await shop.send("ali")
    assert shop.last_text == report_plain(texts.reseller_label_phone_prompt.format(name="ali"))
    assert shop.buttons == [texts.reseller_label_skip_phone]
    # ۷ب.۱۳ب: the phone makes the label «name | phone»; the panel name is «name_xxxx».
    mark = shop.mark()
    await shop.send("0912-345-6789")
    first = await app.latest_order(shop)
    assert first.customer_label == "ali | 09123456789" and first.price_toman == 0
    assert (
        report_plain(
            texts.reseller_trial_free.format(days="1", gb="1", group="ویژه", remaining="1")
        )
        in shop.texts_since(mark)[-1]
    )
    assert [u for u in app.panel.users if re.fullmatch(r"ali_[a-z0-9]{4}", u)]

    # ۷ب.۱۳ج / ۷ب.۱۴: «no number» leaves a bare name; still free, now «0 left».
    mark = shop.mark()
    await take_trial(shop, "ویژه", "ali2", None)
    second = await app.latest_order(shop)
    assert second.customer_label == "ali2" and second.price_toman == 0
    assert (
        report_plain(
            texts.reseller_trial_free.format(days="1", gb="1", group="ویژه", remaining="0")
        )
        in shop.texts_since(mark)[-1]
    )
    assert await _wallet(app, shop.user_id) == WALLET

    # ۷ب.۱۵: the third is made, 5000 comes off the wallet, and the shop is told.
    mark = shop.mark()
    await take_trial(shop, "ویژه", "ali3", None)
    third = await app.latest_order(shop)
    assert third.price_toman == EXTRA
    assert await _wallet(app, shop.user_id) == WALLET - EXTRA == 3000
    assert (
        report_plain(
            texts.reseller_trial_charged.format(days="۱", gb="۱", group="ویژه", price="۵٬۰۰۰")
        )
        in shop.texts_since(mark)[-1]
    )

    # ۷ب.۱۶: with 3000 left the fourth is refused, in words, and costs nothing.
    mark = shop.mark()
    await take_trial(shop, "ویژه", "ali4", None)
    assert report_plain(texts.reseller_trial_no_funds.format(price="۵٬۰۰۰")) in shop.texts_since(
        mark
    )
    assert (await app.latest_order(shop)).id == third.id
    assert await _wallet(app, shop.user_id) == 3000

    # ۷ب.۱۷: three different panel usernames, not one account returned three times.
    names = sorted(app.panel.users)
    assert len(names) == 3
    assert sorted(n.rsplit("_", 1)[0] for n in names) == ["ali", "ali2", "ali3"]
    assert len({u.id for u in app.panel.users.values()}) == 3
    assert len(app.panel.requests_to("POST /api/user")) == 3


@smoke("۷ب.۱۹")
async def test_giving_trials_away_does_not_use_the_shops_own_personal_trial(
    app: App, store: Store
) -> None:
    shop, admin = await make_shop(app, trial=(QUOTA, EXTRA), wallet=WALLET)
    await take_trial(shop, "ویژه", "ali", None)
    await open_card(admin, shop.name)
    trial_line = next(line for line in admin.last_text.splitlines() if line.startswith("تست:"))
    assert "✓" not in trial_line and trial_line.count("—") == 2, trial_line
    user = await app.one(select(User).where(User.telegram_id == shop.user_id))
    assert user.trial_claimed_at is None
    # ... and the shop can still take its own, as a customer would.
    await shop.tap_reply("🎟 تست رایگان")
    assert shop.last_text == report_plain(texts.reseller_trial_pick_group)


@smoke("۷ب.۱۸", "۷ب.۲۰", "۷ب.۲۱")
async def test_reports_and_the_finance_numbers_count_only_money(app: App, store: Store) -> None:
    group = await app.connect_report_group()
    shop, admin = await make_shop(app, trial=(QUOTA, EXTRA), wallet=WALLET)
    for name in ("ali1", "ali2", "ali3"):  # two free, then one charged
        await take_trial(shop, "ویژه", name, None)
    await app.run_job("report_flush")

    # ۷ب.۱۸: one message per trial in the customers topic.
    issued = [t for t in await group.texts("customers") if t.startswith("🎁 تست صادر شد")]
    assert len(issued) == 3, issued
    for name in ("ali1", "ali2", "ali3"):
        assert sum(bool(re.search(f"نام روی سرویس: {name}(\\n|$)", t)) for t in issued) == 1

    # ۷ب.۲۰: only the charged trial is in the sales topic.
    sales = [t for t in await group.texts("sales") if "تست مسیر" not in t]
    assert len(sales) == 1 and "ali3" in sales[0], sales
    assert "5000" in digits(sales[0])

    # ۷ب.۲۱: the finance report counts one order worth 5000, not three.
    await admin.send("/admin")
    await admin.tap("فروش و سفارش")
    await admin.tap("گزارش مالی")
    today = next(line for line in admin.last_text.splitlines() if line.startswith("امروز"))
    assert re.match(r"امروز: 5000\s+\(1 سفارش", digits(today)), today
