"""Findings of docs/ARCH_AUDIT_2026-10_FA.md, written as the behaviour the owner needs.

Each one asserts the correct behaviour. A finding the bot had not fixed yet carried a strict
xfail until it was; none is left here.
"""

from __future__ import annotations

from sqlalchemy import select

from bot.keyboards.reply_menu import SUPPORT
from bot.texts import texts
from database.models import Order, WalletEntryType, WalletLedgerEntry
from tests.e2e.harness import App, browse_to_plan, order_by_card, plain, publish_store, smoke


@smoke("ARCH-1.2", "۴.۷")
async def test_receipt_after_tapping_support_still_reaches_the_admins(app: App) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await order_by_card(ali, store)
    await ali.tap_reply(SUPPORT)  # reads the card, wonders, taps support...
    admin = app.admin()
    await admin.send("/start")
    before = len(admin.photos)
    sent = await ali.send_photo()  # ...then pays and sends the receipt
    assert len(admin.photos) == before + 1, "the admins never got the receipt"
    assert admin.photos[-1].file_id == sent


@smoke("ARCH-1.8", "۲.۱")
async def test_start_works_for_a_first_name_with_a_less_than_sign(app: App) -> None:
    ali = app.customer("ali", first_name="Ali <3")
    await ali.start()
    assert len(ali.messages) == 1, "the welcome never arrived"
    assert "Ali <3" in ali.last_text


@smoke("ARCH-1.8", "۴.۷")
async def test_receipt_from_a_first_name_with_a_less_than_sign_reaches_the_admins(
    app: App,
) -> None:
    store = await publish_store(app)
    ali = app.customer("ali", first_name="Ali <3")  # no @username: the caption falls back to it
    await ali.start()
    await order_by_card(ali, store)
    admin = app.admin()
    before = len(admin.photos)
    sent = await ali.send_photo()
    assert len(admin.photos) == before + 1, "the admins never got the receipt"
    assert admin.photos[-1].file_id == sent


@smoke("ARCH-1.1")
async def test_panel_500_on_a_wallet_purchase_is_not_charged_twice_or_called_unregistered(
    app: App,
) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 300_000)
    app.panel.fail_next("POST /api/user", status=500, times=None)

    mark = ali.mark()
    for _attempt in range(2):  # the bot says «یه بار دیگه امتحان کن», so the customer does
        await browse_to_plan(ali, store)
        await ali.send("ali12")
        await ali.tap("بدون کد تخفیف")
        await ali.tap("ثبت سفارش")

    debits = await app.all(
        select(WalletLedgerEntry).where(WalletLedgerEntry.entry_type == WalletEntryType.debit)
    )
    problems = []
    if len(debits) > 1:
        problems.append(f"charged {len(debits)} times: {[d.amount_toman for d in debits]}")
    if plain(texts.purchase_error) in ali.texts_since(mark):
        problems.append("the customer was told the order was not registered")
    if len(await app.all(select(Order))) != 1:
        problems.append("a second order was created")
    assert not problems, "; ".join(problems)
