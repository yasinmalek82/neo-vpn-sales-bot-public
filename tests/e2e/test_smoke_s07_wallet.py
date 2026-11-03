"""Section 7: the owner's customer list, the customer card and the wallet.

The first half is the admin's side (list, filters, card, ledger, DM, zeroing,
trial reset); the second half is the customer spending the wallet
(`test_smoke_s07_wallet_spend.py`).

Row ids repeat inside the checklist (۷.۲ to ۷.۷ appear twice, once for the list
and once for the wallet); the scenario names say which one is meant.
"""

from __future__ import annotations

import re

import pytest

from bot.keyboards.reply_menu import ACCOUNT, BUY
from bot.texts import texts
from services.customer_lookup import USER_FILTER_LABELS, UserFilter
from services.ux_format import to_persian_digits
from tests.e2e._s07_helpers import (
    admin_changes_wallet,
    card_value,
    credit_by_service,
    db_user,
    ledger,
    list_names,
    open_customer_card,
    press_on_last,
    row_name,
    take_trial,
    users_list,
)
from tests.e2e.harness import App, Store, digits, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

FILTER = dict(USER_FILTER_LABELS)


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _ban(app: App, admin, telegram_id: int) -> None:
    await admin.send("/admin")
    await admin.tap("👥 مشتری‌ها")
    await admin.tap("🚫 مسدود کردن")
    await admin.send(str(telegram_id))


async def _rich_customer(app: App, store: Store, admin):
    """ali: referred bob, +300000 and -10000 by the owner, then bought a plan from the wallet."""
    ali = app.customer("ali", username="alibaba")
    await ali.start()
    code = (await db_user(app, ali)).referral_code
    bob = app.customer("bob", username="bobby")
    await bob.start(code)
    await admin_changes_wallet(admin, "ali", 300_000)
    await admin_changes_wallet(admin, "ali", -10_000)
    await ali.tap_reply(BUY)
    await ali.tap("حجمی")
    await ali.tap("اقتصادی")
    await ali.tap(store.plans["economy_50"].title)
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    await ali.tap("ثبت سفارش")
    return ali, bob


# ---------------------------------------------------------------- the list


@smoke("۷.۱", "۷.۱ب")
async def test_the_users_list_opens_on_a_list_and_a_row_opens_the_card(
    app: App, store: Store
) -> None:
    ali = app.customer("ali", username="alibaba")
    await ali.start()
    bob = app.customer("bob")
    await bob.start()
    await credit_by_service(app, ali, 200_000)
    await take_trial(ali)
    admin = app.admin()
    await users_list(admin)
    assert admin.last_text != report_plain(
        texts.admin_customer_prompt
    ), "the list opened a search prompt"
    names = {row_name(n) for n in list_names(admin)}
    assert {"ali", "bob"} <= names, admin.rows
    ali_row = next(n for n in list_names(admin) if row_name(n) == "ali")
    bob_row = next(n for n in list_names(admin) if row_name(n) == "bob")
    assert "۲۰۰ هزار" in ali_row and "📱 ۱" in ali_row, ali_row
    assert bob_row == "bob", "an empty wallet and no service add nothing to the row"
    await press_on_last(admin, ali_row)
    assert admin.last_text.startswith("👤 ali")


@smoke("۷.۱الف")
async def test_paging_walks_forward_and_back_without_repeats_or_gaps(app: App) -> None:
    people = []
    for i in range(12):
        person = app.customer(f"u{i:02d}")
        await person.start()
        people.append(person)
    admin = app.admin()
    await users_list(admin)
    page_one = [row_name(n) for n in list_names(admin)]
    assert len(page_one) == 10 and "بعدی ➡️" in admin.buttons and "⬅️ قبلی" not in admin.buttons
    await admin.tap("بعدی")
    page_two = [row_name(n) for n in list_names(admin)]
    assert "⬅️ قبلی" in admin.buttons and "بعدی ➡️" not in admin.buttons
    everyone = {p.name for p in people} | {"Admin900001"}
    assert sorted(page_one + page_two) == sorted(everyone), (page_one, page_two)
    await admin.tap("⬅️ قبلی")
    assert [row_name(n) for n in list_names(admin)] == page_one


@smoke("۷.۱ج")
async def test_search_by_numeric_id_from_inside_the_list(app: App) -> None:
    ali = app.customer("ali", username="alibaba")
    await ali.start()
    admin = app.admin()
    await users_list(admin)
    await admin.tap("🔎 جستجو با آیدی")
    assert admin.last_text == report_plain(texts.admin_customer_prompt)
    await admin.send(str(ali.user_id))
    assert admin.last_text.startswith("👤 ali")
    assert f"آیدی {ali.user_id}" in admin.last_text


@smoke("۷.۲")
async def test_search_by_telegram_username_finds_the_card(app: App) -> None:
    ali = app.customer("ali", username="alibaba")
    await ali.start()
    admin = app.admin()
    await users_list(admin)
    await admin.tap("🔎 جستجو با آیدی")
    await admin.send("@alibaba")
    assert admin.last_text.startswith("👤 ali") and "@alibaba" in admin.last_text


@smoke("۷.۲")
async def test_each_filter_shows_only_its_own_people_and_counts_them(
    app: App, store: Store
) -> None:
    """ali has a service and a referral; bob has money; carol is banned; dave is nobody."""
    admin = app.admin()
    ali = app.customer("ali")
    await ali.start()
    code = (await db_user(app, ali)).referral_code
    bob = app.customer("bob")
    await bob.start(code)
    carol = app.customer("carol")
    await carol.start()
    dave = app.customer("dave")
    await dave.start()
    await take_trial(ali)
    await credit_by_service(app, bob, 50_000)
    await _ban(app, admin, carol.user_id)

    expected = {
        UserFilter.ALL: {"Admin900001", "ali", "bob", "carol", "dave"},
        UserFilter.WITH_BALANCE: {"bob"},
        UserFilter.WITH_REFERRALS: {"ali"},
        UserFilter.WITH_SERVICE: {"ali"},
        UserFilter.NO_SERVICE: {"Admin900001", "bob", "carol", "dave"},
        UserFilter.BANNED: {"carol"},
    }
    await users_list(admin)
    for user_filter, people in expected.items():
        if user_filter is not UserFilter.ALL:
            await press_on_last(admin, FILTER[user_filter])
        shown = {row_name(n) for n in list_names(admin)}
        assert shown == people, (user_filter, shown)
        active = [b for row in admin.rows for b in row if b.startswith("● ")]
        assert active == [f"● {FILTER[user_filter]}"], (user_filter, active)
        header = texts.admin_users_title.format(
            label=FILTER[user_filter],
            total=to_persian_digits(len(people)),
            first=to_persian_digits(1),
            last=to_persian_digits(len(people)),
        )
        assert admin.last_text == report_plain(header), (user_filter, admin.last_text)


@smoke("۷.۳")
async def test_an_empty_filter_says_so_and_does_not_claim_there_are_no_users(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await users_list(admin)
    await press_on_last(admin, FILTER[UserFilter.BANNED])
    assert admin.last_text == report_plain(texts.admin_users_filter_empty.format(label="مسدود"))
    assert admin.last_text != report_plain(texts.admin_users_empty)
    assert "● مسدود" in admin.buttons


@smoke("۷.۴")
async def test_next_page_keeps_the_filter(app: App, store: Store) -> None:
    people = []
    for i in range(12):
        person = app.customer(f"u{i:02d}")
        await person.start()
        people.append(person)
    await take_trial(people[0])
    await take_trial(people[1])
    admin = app.admin()
    await users_list(admin)
    await press_on_last(admin, FILTER[UserFilter.NO_SERVICE])
    first = [row_name(n) for n in list_names(admin)]
    await admin.tap("بعدی")
    second = [row_name(n) for n in list_names(admin)]
    assert "بدون سرویس" in admin.last_text.splitlines()[0]
    assert "● بدون سرویس" in admin.buttons
    # 13 people (twelve customers and the admin), two with a service: eleven wait.
    assert len(first) + len(second) == 11, (first, second)
    assert {"u00", "u01"}.isdisjoint(first + second), "someone with a service leaked in"
    assert len(set(first + second)) == 11


@smoke("۷.۵")
async def test_a_customer_with_two_services_is_listed_once_under_service_holders(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await take_trial(ali, "ویژه")
    await take_trial(ali, "نامحدود")
    assert len(app.panel.users) == 2
    admin = app.admin()
    await users_list(admin)
    await press_on_last(admin, FILTER[UserFilter.WITH_SERVICE])
    assert [row_name(n) for n in list_names(admin)] == ["ali"]
    assert "1 نفر" in admin.last_text


# ---------------------------------------------------------------- the card


@smoke("۷.۶", "۷.۷", "۷.۸", "۷.۹")
async def test_the_card_has_every_field_reconciles_and_reads_in_persian(
    app: App, store: Store
) -> None:
    admin = app.admin()
    ali, bob = await _rich_customer(app, store, admin)
    await open_customer_card(admin, "ali")
    card = admin.last_text
    for field in (
        "عضویت:",
        "آخرین فعالیت:",
        "تست:",
        "کیف پول:",
        "مجموع شارژ:",
        "مجموع خرید:",
        "زیرمجموعه:",
        "معرف:",
        "کد دعوت:",
        "سرویس‌ها:",
    ):
        assert field in card, f"{field!r} missing from the card:\n{card}"

    # ۷.۷: charged minus bought minus deducted is the balance on the card.
    charged = card_value(card, "مجموع شارژ:")
    bought = card_value(card, "مجموع خرید:")
    balance = card_value(card, "کیف پول:")
    deducted = sum(
        e.amount_toman for e in await ledger(app, ali) if e.reason == "admin_manual_debit"
    )
    assert deducted == 10_000
    assert (charged, bought, balance) == (300_000, 90_000, 200_000)
    assert charged - bought - deducted == balance

    # ۷.۸: delivered report numbers use ASCII; copied codes/handles keep their identity.
    for line in card.splitlines():
        if line.startswith(("@", "کد دعوت")) or "آیدی" in line:
            continue
        assert not re.search(r"[۰-۹٠-٩]", line), f"a non-ASCII digit on the card: {line!r}"

    # ۷.۹: the owner's own adjustments read as Persian reasons.
    assert "شارژ دستی ادمین" in card and "کسر دستی ادمین" in card
    assert "admin_manual" not in card


@smoke("۷.۹")
async def test_a_purchase_debit_is_labelled_in_persian_on_the_card(app: App, store: Store) -> None:
    admin = app.admin()
    await _rich_customer(app, store, admin)
    await open_customer_card(admin, "ali")
    ledger_lines = admin.last_text.split(texts.admin_customer_ledger_title)[1].splitlines()
    assert ledger_lines, admin.last_text
    for line in ledger_lines:
        assert not re.search(r"[a-z]{3,}_[a-z]+", line), f"a raw reason code: {line!r}"


@smoke("۷.۱۰")
async def test_orders_button_lists_orders_with_persian_status_and_jalali_date(
    app: App, store: Store
) -> None:
    admin = app.admin()
    ali, _ = await _rich_customer(app, store, admin)
    await open_customer_card(admin, "ali")
    await press_on_last(admin, "🛍 سفارش‌ها")
    orders = [b for row in admin.rows for b in row if b.startswith("#")]
    assert len(orders) == 1, admin.rows
    assert "فعال" in orders[0]
    assert re.search(r"۱۴\d\d/\d\d/\d\d", orders[0]), orders[0]
    assert "ACTIVE" not in orders[0]


@smoke("۷.۱۱")
async def test_referrals_button_lists_the_people_who_came_by_the_link(
    app: App, store: Store
) -> None:
    admin = app.admin()
    ali, bob = await _rich_customer(app, store, admin)
    await open_customer_card(admin, "ali")
    await press_on_last(admin, "🎁 زیرمجموعه‌ها")
    assert admin.last_text.startswith("🎁 زیرمجموعه‌های این کاربر: 1 نفر")
    await press_on_last(admin, "bob")
    assert admin.last_text.startswith("👤 bob") and "معرف: @alibaba" in admin.last_text


# -------------------------------------------------------- DM, zeroing, trial


@smoke("۷.۱۲")
async def test_a_message_to_the_customer_reaches_their_chat_and_confirms_to_the_owner(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("✍️ پیام")
    mark = ali.mark()
    await admin.send("سلام، لینکت رو عوض کردم")
    assert ali.texts_since(mark) == ["سلام، لینکت رو عوض کردم"]
    assert admin.last_text == report_plain(texts.admin_customer_dm_sent)


@smoke("۷.۱۲")
async def test_a_short_message_that_mentions_the_service_is_still_sent_to_the_customer(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("✍️ پیام")
    mark = ali.mark()
    await admin.send("سرویست فعال شد")
    assert ali.texts_since(mark) == ["سرویست فعال شد"]
    assert admin.last_text == report_plain(texts.admin_customer_dm_sent)


@smoke("۷.۱۳")
async def test_a_message_to_someone_who_blocked_the_bot_says_it_did_not_go(app: App) -> None:
    ali = app.customer("ali")
    await ali.start()
    app.tg.block(ali.user_id)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("✍️ پیام")
    await admin.send("سلام")
    assert admin.last_text == report_plain(texts.admin_customer_dm_failed)
    assert admin.rows, "the owner is left with a way out"
    assert "Traceback" not in admin.last_text and "Forbidden" not in admin.last_text


@smoke("۷.۱۴", "۷.۱۵")
async def test_zeroing_asks_twice_names_the_amount_and_leaves_a_ledger_row(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 200_000)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("0️⃣ صفر کردن کیف پول")
    assert "200,000" in admin.last_text and "صفر" in admin.last_text
    assert (await db_user(app, ali)).wallet_balance_toman == 200_000, "nothing moved yet"
    assert admin.rows[0] == ["✅ بله، صفر کن", "انصراف"]
    await admin.tap("✅ بله، صفر کن")
    assert admin.popup == texts.admin_wallet_zero_done.format(amount="200,000")
    assert (await db_user(app, ali)).wallet_balance_toman == 0
    entries = await ledger(app, ali)
    assert [(e.entry_type.value, e.amount_toman) for e in entries] == [
        ("credit", 200_000),
        ("debit", 200_000),
    ]
    assert entries[-1].balance_after_toman == 0
    assert "−200,000" in admin.last_text, "the card shows the deduction row"
    assert "0️⃣" not in " ".join(admin.buttons), "an empty wallet offers nothing to zero"


@smoke("۷.۱۶")
async def test_zeroing_an_empty_wallet_says_it_is_already_empty(app: App) -> None:
    """The card hides the button on an empty wallet, so the owner reaches this through a stale one."""
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 50_000)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    stale_card = admin.last_message
    stale_button = next(
        b.callback_data
        for row in stale_card.markup.inline_keyboard
        for b in row
        if b.text.startswith("0️⃣")
    )
    await admin.tap("0️⃣ صفر کردن کیف پول")
    await admin.tap("✅ بله، صفر کن")
    await admin.press(stale_button, message=stale_card)
    assert admin.last_text == report_plain(texts.admin_wallet_zero_already)
    assert len(await ledger(app, ali)) == 2, "a second zeroing wrote nothing"


@smoke("۷.۱۷")
async def test_trial_reset_lets_them_test_again_and_keeps_the_old_service(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await take_trial(ali)
    first = app.panel.only_user().username
    admin = app.admin()
    await open_customer_card(admin, "ali")
    assert "ویژه ✓" in admin.last_text
    await admin.tap("🧪 اجازهٔ تست دوباره")
    assert admin.popup == texts.admin_customer_trial_reset_done
    assert "ویژه —" in admin.last_text
    assert "🧪" not in " ".join(admin.buttons), "nothing left to reset"
    await take_trial(ali)
    assert "تست رایگان فعال شد" in ali.last_text
    assert len(app.panel.users) == 2 and first in app.panel.users, "the first service is gone"
    await ali.tap_reply(ACCOUNT)
    assert ali.buttons.count("🟡 تست ویژه") == 2, ali.buttons


@smoke("۷.۱۸")
async def test_no_reset_button_for_someone_who_never_took_a_trial(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    admin = app.admin()
    await open_customer_card(admin, "ali")
    assert "🧪" not in " ".join(admin.buttons), admin.buttons


# ---------------------------------------------------------------- the ledger


@smoke("۷.۳")
async def test_owner_credit_changes_the_balance_everywhere(app: App) -> None:
    ali = app.customer("ali", username="alibaba")
    await ali.start()
    admin = app.admin()
    await admin_changes_wallet(admin, "ali", 200_000)
    assert (await db_user(app, ali)).wallet_balance_toman == 200_000
    assert "کیف پول: 200,000" in admin.last_text
    await ali.tap_reply("🏦 کیف پول")
    assert "200000" in digits(ali.last_text)


@smoke("۷.۷")
async def test_a_manual_deduction_above_the_balance_is_refused_and_nothing_goes_negative(
    app: App,
) -> None:
    ali = app.customer("ali")
    await ali.start()
    await credit_by_service(app, ali, 30_000)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("🏦 کیف پول")
    await admin.send("-50000")
    assert admin.last_text == report_plain(texts.admin_wallet_insufficient.format(wallet="30,000"))
    assert (await db_user(app, ali)).wallet_balance_toman == 30_000
    entries = await ledger(app, ali)
    assert [e.amount_toman for e in entries] == [30_000]
    assert "تأیید و اعمال" not in " ".join(admin.buttons), "no confirm for a refused amount"
