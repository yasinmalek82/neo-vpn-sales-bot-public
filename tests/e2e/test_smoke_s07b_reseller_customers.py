"""Section ۷ب, part 4: «مشتری‌های من», search, the customer's card, a second shop.

Rows: ۷ب.۲۲ to ۷ب.۲۸. The shop's customers are made with free trials (the quota is
generous), which put the same rows on the same screens as paid services do.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.texts import texts
from database.models import Order
from tests.e2e._s07b_helpers import (
    enable_reseller_trial,
    list_labels,
    make_customers,
    make_shop,
    tap_starting,
)
from tests.e2e.harness import App, Store, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

MINE = "👥 مشتری‌های من"
TRIALS = (40, 0)


@pytest.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await enable_reseller_trial(app, s, "unlimited", "special")
    return s


@smoke("۷ب.۲۲", "۷ب.۲۷ه")
async def test_the_shops_list_is_named_by_customer_and_the_menu_says_so(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, trial=TRIALS)
    await make_customers(shop, [("reza", "09123456789"), ("ali", None), ("omid", "09350001111")])

    # ۷ب.۲۷ه: the bottom menu says «مشتری‌های من», never «سرویس‌های من».
    assert MINE in shop.reply_buttons
    assert not any("سرویس‌های من" in b for b in shop.reply_buttons), shop.reply_buttons

    await shop.tap_reply(MINE)
    assert shop.last_text == report_plain(
        texts.reseller_services_title.format(total="3", first="1", last="3")
    )
    assert sorted(list_labels(shop)) == ["ali", "omid | 09350001111", "reza | 09123456789"]
    # Labelled by the customer, not by the panel's username (name + 4 random characters).
    assert not any(user in b for user in app.panel.users for b in shop.buttons)


@smoke("۷ب.۲۳")
async def test_the_second_page_shows_the_rest_without_repeats(app: App, store: Store) -> None:
    shop, _ = await make_shop(app, trial=TRIALS)
    names = [f"cust{i:02d}" for i in range(12)]
    await make_customers(shop, [(n, None) for n in names])
    await shop.tap_reply(MINE)
    first_page = list_labels(shop)
    assert len(first_page) == 10
    assert report_plain(texts.reseller_services_title.format(total="12", first="1", last="10")) == (
        shop.last_text
    )
    await shop.tap("بعدی ➡️")
    second_page = list_labels(shop)
    assert len(second_page) == 2
    assert sorted(first_page + second_page) == sorted(names)


@smoke("۷ب.۲۴", "۷ب.۲۴الف", "۷ب.۲۴ب", "۷ب.۲۴ج", "۷ب.۲۵")
async def test_search_by_name_by_last_four_digits_in_either_script_and_a_miss(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, trial=TRIALS)
    # The phone is typed with dashes: they are cleaned when it is stored (۷ب.۲۴ج).
    await make_customers(
        shop,
        [
            ("ali", "0912-345-6789"),
            ("ali2", "09121116789"),
            ("reza", "09123330000"),
        ],
    )
    await shop.tap_reply(MINE)

    async def search(query: str) -> None:
        await shop.tap("🔎 جستجوی مشتری")
        assert shop.last_text == report_plain(texts.reseller_services_search_prompt)
        await shop.send(query)

    # ۷ب.۲۴: a piece of a name: only those, with the count.
    await search("ali")
    assert shop.last_text == report_plain(
        texts.reseller_services_found.format(query="ali", total="2")
    )
    assert sorted(list_labels(shop)) == ["ali | 09123456789", "ali2 | 09121116789"]
    # ۷ب.۲۴الف / ۲۴ج: the last four digits find everyone who has them, dashes or not.
    await search("6789")
    assert shop.last_text == report_plain(
        texts.reseller_services_found.format(query="6789", total="2")
    )
    english = sorted(list_labels(shop))
    assert english == ["ali | 09123456789", "ali2 | 09121116789"]
    # ۷ب.۲۴ب: the same four digits in Persian digits give the same result.
    await search("۶۷۸۹")
    assert shop.last_text == report_plain(
        texts.reseller_services_found.format(query="6789", total=str(2))
    )
    assert sorted(list_labels(shop)) == english
    # A number that belongs to one customer finds one.
    await search("0000")
    assert list_labels(shop) == ["reza | 09123330000"]
    # ۷ب.۲۵: a miss says so; it does not quietly return everyone.
    await search("zzzz")
    assert shop.last_text == report_plain(texts.reseller_services_none_found.format(query="zzzz"))
    assert list_labels(shop) == []


@smoke("۷ب.۲۶")
async def test_all_services_returns_from_a_search_to_the_full_list(app: App, store: Store) -> None:
    shop, _ = await make_shop(app, trial=TRIALS)
    await make_customers(shop, [("ali", None), ("reza", None), ("omid", None)])
    await shop.tap_reply(MINE)
    await shop.tap("🔎 جستجوی مشتری")
    await shop.send("ali")
    assert list_labels(shop) == ["ali"]
    await shop.tap("✖️ همهٔ سرویس‌ها")
    assert sorted(list_labels(shop)) == ["ali", "omid", "reza"]


@smoke("۷ب.۲۷", "۷ب.۲۷الف", "۷ب.۲۷ب", "۷ب.۲۷ج")
async def test_a_customers_card_names_the_customer_and_the_label_can_change(
    app: App, store: Store
) -> None:
    shop, _ = await make_shop(app, trial=TRIALS)
    await make_customers(shop, [("reza", "09123456789")])
    await shop.tap_reply(MINE)
    await tap_starting(shop, "🟡 reza")

    # ۷ب.۲۷ / ۲۷الف: the service card (with its QR), the two label lines on top, the right title.
    card = shop.last_message
    assert card.kind == "photo" and card.file_bytes and card.file_bytes.startswith(b"\x89PNG")
    panel_username = app.panel.only_user().username
    lines = shop.last_text.splitlines()
    assert lines[0] == "👤 مشتری: reza | 09123456789"
    assert lines[1] == f"📡 سرویس: {panel_username}"
    assert report_plain(texts.account_overview_title_customer) in shop.last_text
    assert "سرویس تو" not in shop.last_text
    # A trial has no renew (plans/2026-10-04, ۱.۲): «🛒 خرید اشتراک» takes its place.
    assert "📅 تمدید این سرویس" not in shop.buttons and "🛒 خرید اشتراک" in shop.buttons
    assert "✏️ اسم/شمارهٔ مشتری" in shop.buttons
    assert f"https://panel.test/sub/{app.panel.only_user().token}" in shop.last_text
    assert "حجم مصرف‌شده" in shop.last_text

    # ۷ب.۲۷ب: the same two steps again, name then phone.
    await shop.tap("✏️ اسم/شمارهٔ مشتری")
    assert shop.last_text.startswith(report_plain(texts.reseller_label_name_prompt))
    assert (
        report_plain(texts.reseller_rename_prompt.format(current="reza | 09123456789"))
        in shop.last_text
    )
    await shop.send("Reza M")
    assert shop.last_text == report_plain(texts.reseller_label_phone_prompt.format(name="reza_m"))
    await shop.send("09121112222")

    # ۷ب.۲۷ج: the list label changes; the panel username does not.
    assert shop.last_text.startswith(
        report_plain(
            texts.reseller_services_title.format(total="۱", first="۱", last="۱")
        ).splitlines()[0]
    )
    assert list_labels(shop) == ["reza_m | 09121112222"]
    assert list(app.panel.users) == [panel_username]
    orders = await app.all(select(Order))
    assert [o.panel_username for o in orders] == [panel_username]
    assert app.panel.requests_to("PUT /api/user/{username}") == []


@smoke("۷ب.۲۷د")
async def test_an_ordinary_customer_has_no_customer_line_and_no_rename_button(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply("🎟 تست رایگان")
    await tap_starting(ali, "ویژه")
    await ali.tap_reply("📡 سرویس‌های من")
    await tap_starting(ali, "🟡")
    assert report_plain(texts.account_overview_title_own) in ali.last_text
    assert "مشتری:" not in ali.last_text
    assert "✏️ اسم/شمارهٔ مشتری" not in ali.buttons
    assert not any("اسم/شماره" in b for b in ali.buttons)


@smoke("۷ب.۲۸")
async def test_a_second_shop_sees_only_its_own_customers(app: App, store: Store) -> None:
    shop_a, _ = await make_shop(app, "shopa", trial=TRIALS)
    shop_b, _ = await make_shop(app, "shopb", trial=TRIALS)
    await make_customers(shop_a, [("amir", None), ("arash", None)])
    await make_customers(shop_b, [("bahar", None)])
    await shop_a.tap_reply(MINE)
    assert sorted(list_labels(shop_a)) == ["amir", "arash"]
    await shop_b.tap_reply(MINE)
    assert list_labels(shop_b) == ["bahar"]
    assert shop_b.last_text == report_plain(
        texts.reseller_services_title.format(total="1", first="1", last="1")
    )
    # A name that belongs to the other shop finds nothing.
    await shop_b.tap("🔎 جستجوی مشتری")
    await shop_b.send("amir")
    assert shop_b.last_text == report_plain(texts.reseller_services_none_found.format(query="amir"))
