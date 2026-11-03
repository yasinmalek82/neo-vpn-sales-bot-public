"""End-to-end tests for the panel sale gate (Milestone 1).

Tests every refused entry point under confirmed outage: new purchase (menu,
bottom keyboard, restart), renewal, plan switch, custom purchase builder,
confirm order (card, wallet, zero price), personal trial, reseller trial
(started before the outage, label sent after it).

Tests fail-open paths (1 check down, stale row),
open paths during outage (receipt upload, wallet topup, admin approval),
and report text verification in the system outbox.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

import bot.handlers.purchase as purchase_handlers
from bot.keyboards.reply_menu import BUY
from bot.texts import texts
from database.models import Order, OrderStatus, PanelNodeState, WalletEntryType, WalletLedgerEntry
from database.models.panel_node_state import PANEL_NODE_ID
from services.pricing import PriceResult
from tests.e2e._s07b_helpers import (
    add_custom_volume,
    approve_order,
    enable_reseller_trial,
    make_shop,
    tap_starting,
)
from tests.e2e._s08_helpers import buy_active, my_service
from tests.e2e._s14_helpers import system_texts, watch
from tests.e2e.harness import Actor, App, Store, plain, publish_store, smoke

RESELLER_WALLET = 8000
RESELLER_TRIAL_QUOTA = (2, 5000)


@pytest_asyncio.fixture
async def store(app: App) -> Store:
    s = await publish_store(app)
    await enable_reseller_trial(app, s, "unlimited", "special")
    return s


async def set_panel_confirmed_down(app: App, *, checks: int = 2, age_seconds: int = 10) -> None:
    """Directly configure confirmed panel down state in database."""
    now = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with app.session() as session:
        row = await session.get(PanelNodeState, PANEL_NODE_ID)
        if row is None:
            row = PanelNodeState(
                node_id=PANEL_NODE_ID,
                name="panel",
                status="unreachable",
                checks_in_status=checks,
                status_since=now,
                last_checked_at=now,
            )
            session.add(row)
        else:
            row.status = "unreachable"
            row.checks_in_status = checks
            row.last_checked_at = now
        await session.commit()


def assert_refused_in_message(who: Actor) -> None:
    """The handler had already answered the tap, so the customer gets the full message only."""
    assert who.last_text == plain(texts.purchase_paused_panel)
    assert who.popup is None


# ── 1. Outage Refusal Tests ────────────────────────────────


@smoke("۱۴.۹")
async def test_outage_refuses_new_buy(app: App, store: Store) -> None:
    """[M1-MUT-3] Entry Point 1: Starting new purchase flow is refused.

    No order created, refusal copy delivered.
    """
    await set_panel_confirmed_down(app)
    ali = app.customer("ali")
    await ali.start()

    await ali.tap_reply(BUY)

    assert_refused_in_message(ali)
    # Asserts no shelf shown
    assert store.groups["volume"].title not in ali.buttons
    # Invariant: zero orders in database
    assert await app.all(select(Order)) == []


@smoke("۱۴.۹")
async def test_outage_refuses_renew(app: App, store: Store) -> None:
    """[M1-MUT-4] Entry Point 2: Starting renewal flow is refused.

    Setup active service while panel is up; then outage hits.
    """
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    initial_orders = await app.all(select(Order))
    assert len(initial_orders) == 1

    await set_panel_confirmed_down(app)

    await my_service(ali)
    await tap_starting(ali, "تمدید این سرویس")

    assert_refused_in_message(ali)
    # Invariant: order count unchanged (no renewal order created)
    assert len(await app.all(select(Order))) == 1


@smoke("۱۴.۹")
async def test_outage_refuses_plan_switch(app: App, store: Store) -> None:
    """[M1-MUT-5] Entry Point 3: Starting plan switch is refused."""
    ali = app.customer("ali")
    await buy_active(app, store, ali)

    await my_service(ali)
    await tap_starting(ali, "تمدید این سرویس")
    await tap_starting(ali, "بدون کد تخفیف")
    assert "🔁 پلن دیگه روی همین سرویس" in ali.buttons

    # Outage strikes right before tapping plan switch
    await set_panel_confirmed_down(app)

    await tap_starting(ali, "🔁 پلن دیگه روی همین سرویس")

    assert_refused_in_message(ali)
    assert len(await app.all(select(Order))) == 1


@smoke("۱۴.۹")
async def test_outage_refuses_custom_purchase(app: App, store: Store) -> None:
    """[M1-MUT-6] Entry Point 4: Starting custom plan builder is refused."""
    await add_custom_volume(app, store)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    assert "🧩 خرید سفارشی" in ali.buttons

    # Outage strikes right before tapping custom plan builder
    await set_panel_confirmed_down(app)

    await tap_starting(ali, "🧩 خرید سفارشی")

    # The custom builder's guard is the first thing the handler does: popup and message.
    assert ali.popup == plain(texts.purchase_paused_panel_popup)
    assert ali.last_text == plain(texts.purchase_paused_panel)
    assert await app.all(select(Order)) == []


@smoke("۱۴.۹")
async def test_outage_refuses_confirm_order(app: App, store: Store) -> None:
    """[M1-MUT-7] Entry Point 5: Confirming order in checkout is refused.

    User reached summary screen before outage; outage hits right before confirm.
    """
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_service")
    await ali.tap("بدون کد تخفیف")
    assert any("ثبت سفارش" in b for b in ali.buttons)

    # Outage strikes right before confirm!
    await set_panel_confirmed_down(app)

    await ali.tap("✅ ثبت سفارش")

    assert_refused_in_message(ali)
    # Invariant: order was NOT committed
    assert await app.all(select(Order)) == []


@smoke("۱۴.۹")
async def test_outage_refuses_wallet_payment(app: App, store: Store) -> None:
    """[M1-MUT-8] Entry Point 6: Paying order via wallet is refused before debit.

    User has wallet balance; outage strikes; wallet balance remains untouched.
    """
    ali = app.customer("ali")
    await ali.start()
    await app.give_wallet(ali, 500_000)
    initial_balance = await app.wallet_balance(ali)
    assert initial_balance == 500_000

    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_wallet_test")
    await ali.tap("بدون کد تخفیف")
    assert any("ثبت سفارش" in b for b in ali.buttons)

    # Panel goes down before confirmation
    await set_panel_confirmed_down(app)

    await ali.tap("✅ ثبت سفارش")

    assert_refused_in_message(ali)
    # Invariants:
    # 1. Zero orders created
    assert await app.all(select(Order)) == []
    # 2. Wallet balance untouched!
    assert await app.wallet_balance(ali) == initial_balance
    # 3. Only the initial setup credit exists in ledger; no checkout debit added
    ledger_entries = await app.all(select(WalletLedgerEntry))
    assert len(ledger_entries) == 1
    assert ledger_entries[0].entry_type == WalletEntryType.credit
    assert ledger_entries[0].amount_toman == 500_000
    debits = await app.all(
        select(WalletLedgerEntry).where(WalletLedgerEntry.entry_type == WalletEntryType.debit)
    )
    assert debits == []


@smoke("۱۴.۹")
async def test_outage_refuses_personal_trial(app: App, store: Store) -> None:
    """[M1-MUT-9] Entry Point 7: Claiming personal trial is refused."""
    await set_panel_confirmed_down(app)
    ali = app.customer("ali")
    await ali.start()

    await ali.tap_reply("🎟 تست رایگان")

    assert_refused_in_message(ali)
    # Invariants:
    assert await app.all(select(Order)) == []
    assert app.panel.users == {}


@smoke("۱۴.۹")
async def test_outage_refuses_reseller_trial(app: App, store: Store) -> None:
    """[M1-MUT-10] Entry Point 8: Claiming reseller trial is refused."""
    shop, _ = await make_shop(app, trial=RESELLER_TRIAL_QUOTA, wallet=RESELLER_WALLET)
    initial_wallet = await app.wallet_balance(shop)

    await set_panel_confirmed_down(app)

    await shop.tap_reply("🎟 تست رایگان")

    assert_refused_in_message(shop)
    # Invariants:
    assert await app.wallet_balance(shop) == initial_wallet
    assert await app.all(select(Order)) == []
    assert app.panel.users == {}


@smoke("۱۴.۹")
async def test_outage_refuses_restart_from_summary(app: App, store: Store) -> None:
    """«🔁 شروع دوباره» is a way into a new purchase too."""
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_restart")
    await ali.tap("بدون کد تخفیف")
    assert "🔁 شروع دوباره" in ali.buttons

    await set_panel_confirmed_down(app)
    await tap_starting(ali, "🔁 شروع دوباره")

    assert_refused_in_message(ali)
    assert await app.all(select(Order)) == []


@smoke("۱۴.۹")
async def test_outage_refuses_order_with_zero_final_price(
    app: App, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A price of zero needs no card and no wallet debit, and is still a new order."""
    real_quote = purchase_handlers.quote_checkout

    async def free_quote(*args: Any, **kwargs: Any) -> PriceResult:
        return replace(await real_quote(*args, **kwargs), final_price=0)

    monkeypatch.setattr(purchase_handlers, "quote_checkout", free_quote)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_free")
    await ali.tap("بدون کد تخفیف")

    await set_panel_confirmed_down(app)
    await ali.tap("✅ ثبت سفارش")

    assert_refused_in_message(ali)
    assert await app.all(select(Order)) == []
    assert app.panel.users == {}


@smoke("۱۴.۹")
async def test_outage_refuses_reseller_trial_label_sent_after_it_began(
    app: App, store: Store
) -> None:
    """A reseller who started the trial before the outage and answers during it gets nothing made."""
    shop, _ = await make_shop(app, trial=RESELLER_TRIAL_QUOTA, wallet=RESELLER_WALLET)
    initial_wallet = await app.wallet_balance(shop)

    await shop.tap_reply("🎟 تست رایگان")
    await tap_starting(shop, "ویژه")
    await shop.send("Reza")

    await set_panel_confirmed_down(app)
    await shop.tap("شماره ندارم")

    assert shop.last_text == plain(texts.purchase_paused_panel)
    assert await app.wallet_balance(shop) == initial_wallet
    assert await app.all(select(Order)) == []
    assert app.panel.users == {}


@smoke("۱۴.۹")
async def test_outage_still_resumes_an_existing_open_order(app: App, store: Store) -> None:
    """«خرید» with an order already open resumes it; the gate only stops a new one."""
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_resume")
    await ali.tap("بدون کد تخفیف")
    await ali.tap("ثبت سفارش")
    order = await app.latest_order(ali)

    await set_panel_confirmed_down(app)
    await ali.tap_reply(BUY)

    assert plain(texts.purchase_paused_panel) not in ali.last_text
    assert len(await app.all(select(Order))) == 1
    assert (await app.latest_order(ali)).id == order.id


# ── 2. Fail-Open Tests ──────────────────────────────────────────────────────


@smoke("۱۴.۱۰")
async def test_fail_open_single_failed_check_allows_purchase(app: App, store: Store) -> None:
    """Single failed check in watcher must fail open (sales remain open)."""
    # Trigger 1 failed check via watcher
    app.panel.go_down()
    await app.run_job("panel_watch")  # 1 check down

    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)

    # Shelf opens normally!
    assert store.groups["volume"].title in ali.buttons
    assert plain(texts.purchase_paused_panel) not in ali.last_text


@smoke("۱۴.۱۰")
async def test_fail_open_stale_down_row_allows_purchase(app: App, store: Store) -> None:
    """Confirmed down row that is stale (> 3 intervals) must fail open."""
    # Down row aged 400 seconds ago (interval is 120s -> 360s cutoff)
    await set_panel_confirmed_down(app, checks=2, age_seconds=400)

    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(BUY)

    # Sales stay open!
    assert store.groups["volume"].title in ali.buttons
    assert plain(texts.purchase_paused_panel) not in ali.last_text


# ── 3. Open Paths Tests ─────────────────────────────────────────────────────


@smoke("۱۴.۹")
async def test_open_path_receipt_upload_and_admin_approval_during_outage(
    app: App, store: Store
) -> None:
    """Pre-existing open order can submit receipt and be approved during outage."""
    ali = app.customer("ali")
    await ali.start()
    # Order created before outage
    await ali.tap_reply(BUY)
    await ali.tap(store.groups["volume"].title)
    await ali.tap(store.groups["special"].title)
    await ali.tap(store.plans["special_50"].title)
    await ali.send("ali_open_order")
    await ali.tap("بدون کد تخفیف")
    await ali.tap("ثبت سفارش")

    order = await app.latest_order(ali)
    assert order.status == OrderStatus.PENDING_PAYMENT

    # Outage strikes
    await set_panel_confirmed_down(app)

    # 1. Customer uploads receipt for existing order -> succeeds!
    await ali.send_photo()
    async with app.session() as session:
        refreshed = await session.get(Order, order.id)
        assert refreshed is not None
        assert refreshed.status == OrderStatus.PAYMENT_REVIEW

    # 2. Admin approves order -> succeeds!
    await approve_order(app, order.id)
    async with app.session() as session:
        approved = await session.get(Order, order.id)
        assert approved is not None
        assert approved.status == OrderStatus.ACTIVE


@smoke("۱۴.۹")
async def test_open_path_wallet_topup_and_read_only_screens(app: App, store: Store) -> None:
    """Wallet top-up and read-only screens remain open during panel outage."""
    await set_panel_confirmed_down(app)
    ali = app.customer("ali")
    await ali.start()

    # Read-only screens: /help, my services
    await ali.send("/help")
    assert plain(texts.purchase_paused_panel) not in ali.last_text

    # Wallet top-up request
    await ali.tap_reply("🏦 کیف پول")
    assert plain(texts.purchase_paused_panel) not in ali.last_text
    assert "کیف پول من" in ali.last_text


# ── 4. Outbox Reports Tests ─────────────────────────────────────────────────


@smoke("۱۴.۶", "۱۴.۷")
async def test_outage_and_recovery_report_texts_in_outbox(app: App, store: Store) -> None:
    """Verifies that outage report and recovery report texts include sale gate status lines."""
    group = await app.connect_report_group()

    # 1. Outage occurs (2 down checks)
    app.panel.go_down()
    await watch(app, 2)

    alerts = await system_texts(group)
    assert len(alerts) >= 1
    outage_body = alerts[0]
    assert "🔴 پنل در دسترس نیست" in outage_body
    assert "تا پنل جواب نده، سفارش جدید، تمدید و تست رد می‌شه." in outage_body
    # One sentence says it: the old «خرید و تمدید انجام نمی‌شه» line was replaced, not joined.
    assert "تا وصل نشه" not in outage_body

    # 2. Panel recovers (2 connected checks)
    app.panel.go_up()
    await watch(app, 2)

    all_messages = await system_texts(group)
    assert len(all_messages) >= 2
    recovery_body = all_messages[-1]
    assert "🟢 پنل دوباره در دسترس است" in recovery_body
    assert "سفارش جدید، تمدید و تست دوباره باز شد." in recovery_body
