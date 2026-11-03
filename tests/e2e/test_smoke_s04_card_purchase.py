"""Section 4: buying a ready volume plan by card-to-card, from the shelf to the panel.

Rows covered: ۴.۱ to ۴.۱۰, ۴.۱۳ and ۴.۱۴, plus ۴.۱۱ as «what the panel was asked for».
۴.۱۲ (open the link on a phone and browse) is MANUAL: it needs a real VPN client.
"""

from __future__ import annotations

import pytest

from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import OrderStatus
from tests.e2e.harness import (
    GB,
    App,
    Store,
    browse_to_plan,
    digits,
    order_by_card,
    plain,
    publish_store,
    smoke,
)
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.settings import CARD_HOLDER, CARD_NUMBER

PLAN = "special_50"


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


@smoke("۴.۱")
async def test_buy_shows_the_product_groups(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    assert ali.last_text == plain(texts.purchase_choose_type)
    for key in ("unlimited", "volume"):
        assert store.groups[key].title in ali.buttons, ali.buttons
    # A nested group opens onto its children, and the plans sit one level down.
    await ali.tap(store.groups["volume"].title)
    assert [store.groups["special"].title, store.groups["economy"].title] == ali.rows[0]


@smoke("۴.۲")
async def test_a_ready_plan_keeps_its_fixed_price_and_never_asks_for_volume(
    app: App, store: Store
) -> None:
    plan = store.plans[PLAN]
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    assert str(plan.price) in digits(ali.last_message.text + " ".join(ali.buttons))
    await ali.tap(plan.title)
    assert ali.last_text == plain(texts.purchase_enter_service_name)  # a name, not a volume
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    assert str(plan.price) in digits(ali.last_text), "the summary must show the plan's own price"
    assert f"{plan.volume_gb}" in digits(ali.last_text)


@smoke("۴.۳")
async def test_the_name_prompt_shows_examples_not_just_a_rule(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    for example in ("ali12", "mahdi", "ws1_ksdf", "محسن", "علی"):
        assert example in ali.last_text, f"{example!r} missing from the name prompt"


@smoke("۴.۳الف")
async def test_a_dashed_name_is_cleaned_and_the_bot_says_so(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("Ali-Reza")
    assert plain(texts.purchase_service_name_cleaned.format(name="ali_reza")) in ali.texts_since(
        mark
    )


@smoke("۴.۳ب")
async def test_a_persian_name_is_refused_with_the_examples_again(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("محسن")
    assert ali.texts_since(mark) == [plain(texts.purchase_invalid_service_name)]
    for example in ("ali12", "mahdi", "ws1_ksdf"):
        assert example in ali.last_text


@smoke("۴.۳ج")
async def test_a_trailing_underscore_is_accepted_and_cleaned(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("ali_")
    assert plain(texts.purchase_service_name_cleaned.format(name="ali")) in ali.texts_since(mark)
    assert plain(texts.purchase_invalid_service_name) not in ali.texts_since(mark)


@smoke("۴.۴")
async def test_a_persian_only_name_gets_a_persian_refusal(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("علی احمد")
    assert ali.texts_since(mark) == [plain(texts.purchase_invalid_service_name)]


@smoke("۴.۴")
async def test_spaces_in_a_latin_name_are_stripped_and_reported(app: App, store: Store) -> None:
    """Row ۴.۴ says a space is refused; the bot's own prompt promises it removes them."""
    ali = app.customer("ali")
    await ali.start()
    await browse_to_plan(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("ali reza")
    assert plain(texts.purchase_service_name_cleaned.format(name="ali_reza")) in ali.texts_since(
        mark
    )


@smoke("۴.۵")
async def test_confirming_shows_the_card_and_its_holder(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    assert CARD_NUMBER in ali.last_text
    assert CARD_HOLDER in ali.last_text
    assert digits(f"{order.price_toman}") in digits(ali.last_text)
    assert order.status == OrderStatus.PENDING_PAYMENT


@smoke("۴.۶")
async def test_text_instead_of_a_receipt_is_refused(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    mark = ali.mark()
    await ali.send("پرداخت کردم")
    assert ali.texts_since(mark) == [plain(texts.payment_need_photo)]
    assert (await app.latest_order(ali)).status == OrderStatus.PENDING_PAYMENT
    assert order.id == (await app.latest_order(ali)).id


@smoke("۴.۷")
async def test_the_receipt_moves_the_order_to_review(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    mark = ali.mark()
    await ali.send_photo()
    assert ali.texts_since(mark) == [
        report_plain(texts.payment_receipt_received.format(order_id=str(order.id)))
    ]
    assert (await app.latest_order(ali)).status == OrderStatus.PAYMENT_REVIEW


@smoke("۴.۸")
async def test_the_admin_queue_counts_and_lists_the_order(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    waiting = [b for b in admin.buttons if "پرداخت" in b]
    assert len(waiting) == 1 and "1" in digits(waiting[0]), admin.buttons
    await admin.tap(waiting[0])
    assert any(f"#{order.id}" in b for b in admin.buttons), admin.buttons


@smoke("۴.۹")
async def test_show_receipt_again_resends_the_customers_photo(app: App, store: Store) -> None:
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    sent = await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    before = len(admin.photos)
    await admin.tap("نمایش مجدد فیش")
    assert len(admin.photos) == before + 1
    assert admin.photos[-1].file_id == sent
    assert admin.photos[-1].file_bytes == app.tg.files[sent]


@smoke("۴.۱۰", "۴.۱۱")
async def test_approving_delivers_link_and_qr_and_asks_the_panel_for_the_plan(
    app: App, store: Store
) -> None:
    plan = store.plans[PLAN]
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    mark = ali.mark()
    await admin.tap("✅ تایید")

    account = app.panel.only_user()
    # What the customer received: the service card with its QR, and the link.
    arrived = ali.messages_since(mark)
    assert [m.kind for m in arrived] == ["photo"], arrived
    assert account.username in arrived[0].text
    assert f"{app.panel.base_url}/sub/{account.token}" in arrived[0].text
    assert arrived[0].file_bytes and arrived[0].file_bytes.startswith(b"\x89PNG")
    assert (await app.latest_order(ali)).status == OrderStatus.ACTIVE

    # What the panel was asked for (row ۴.۱۱).
    assert account.username.startswith("ali12_")
    assert account.data_limit == plan.volume_gb * GB
    assert 29.9 < (account.days_left() or 0) <= plan.duration_days
    assert account.hwid_limit == 0  # a volume plan sets «نامحدود» explicitly, never None
    assert account.group_ids == store.groups["special"].panel_groups
    assert [r.key for r in app.panel.requests_to("POST /api/user")] == ["POST /api/user"]


@smoke("۴.۱۳")
async def test_sales_and_payments_topics_get_the_amount(app: App, store: Store) -> None:
    plan = store.plans[PLAN]
    group = await app.connect_report_group()
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("✅ تایید")
    await app.run_job("report_flush")
    for topic in ("sales", "payments"):
        real = [t for t in await group.texts(topic) if "تست مسیر" not in t]
        assert len(real) == 1, (topic, real)
        assert str(plan.price) in digits(real[0]), (topic, real[0])


@smoke("۴.۱۴")
async def test_the_finance_report_includes_the_sale_today(app: App, store: Store) -> None:
    plan = store.plans[PLAN]
    ali = app.customer("ali")
    await ali.start()
    order = await order_by_card(ali, store, PLAN)
    await ali.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    await admin.tap("✅ تایید")
    await admin.send("/admin")
    await admin.tap("فروش و سفارش")
    await admin.tap("گزارش مالی")
    today = next(line for line in admin.last_text.splitlines() if line.startswith("امروز"))
    assert str(plan.price) in digits(today), today
    assert "1" in digits(today.split("(")[1])  # one order
