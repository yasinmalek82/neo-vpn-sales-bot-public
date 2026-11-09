"""P0/P1 customer polish: keyboards, texts, delivery renew/howto, renewal job."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.delivery import deliver_provisioned_service_card, send_service_card
from bot.keyboards.account import AccountCB, account_empty_keyboard, service_detail_keyboard
from bot.keyboards.customer import (
    provision_failed_keyboard,
    receipt_waiting_keyboard,
    wallet_actions_keyboard,
)
from bot.keyboards.main_menu import MenuCallback
from bot.texts import texts
from services.accounts import AccountOverview


def _overview(**kwargs) -> AccountOverview:
    base = {
        "has_account": True,
        "has_orders": True,
        "stale": False,
        "account_id": 42,
        "panel_username": "neo_u42ab",
        "display_name": "MySvc",
        "status": "active",
        "expire": datetime.now(tz=UTC) + timedelta(days=2),
        "used_traffic": 1_073_741_824,
        "data_limit": 10_737_418_240,
        "duration_days": 30,
        "subscription_url": "https://sub.example/x",
    }
    base.update(kwargs)
    return AccountOverview(**base)


def test_provision_failed_keyboard_ctas() -> None:
    kb = provision_failed_keyboard()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert MenuCallback.WALLET in cbs
    assert MenuCallback.SUPPORT in cbs
    assert MenuCallback.BUY in cbs
    assert MenuCallback.ACCOUNT in cbs
    assert any("کیف پول" in t for t in labels)
    assert any("پشتیبانی" in t for t in labels)


def test_receipt_waiting_keyboard() -> None:
    kb = receipt_waiting_keyboard()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert AccountCB.BACK in cbs
    assert MenuCallback.ACCOUNT in cbs


def test_wallet_actions_empty_emphasizes_buy() -> None:
    empty = wallet_actions_keyboard(empty=True)
    full = wallet_actions_keyboard(empty=False)
    empty_labels = [b.text for row in empty.inline_keyboard for b in row]
    full_labels = [b.text for row in full.inline_keyboard for b in row]
    assert any("شروع" in t for t in empty_labels)
    assert any(t.startswith("🛒") for t in full_labels)
    assert MenuCallback.BUY in [b.callback_data for row in empty.inline_keyboard for b in row]


def test_empty_services_keyboard_inviting() -> None:
    empty = account_empty_keyboard()
    labels = [b.text for row in empty.inline_keyboard for b in row]
    assert any("اولین اشتراک" in t for t in labels)
    trial = account_empty_keyboard(trial_eligible=True)
    trial_labels = [b.text for row in trial.inline_keyboard for b in row]
    assert any("تست رایگان" in t for t in trial_labels)
    # Trial first when eligible
    assert "تست رایگان" in trial.inline_keyboard[0][0].text


def test_persian_polish_strings() -> None:
    assert "به‌زودی منقضی" in texts.renewal_reminder_intro.format(days=3)
    assert "بررسی دستی ادمین" in texts.payment_receipt_received.format(order_id=1)
    assert "کیف پول" in texts.payment_provision_failed_user.format(order_id=1, amount="۱۰۰")
    assert "صفر" in texts.wallet_empty_hint or "موجودی" in texts.wallet_empty_hint
    assert "📭" in texts.account_services_empty or "سرویس" in texts.account_services_empty
    assert "بررسی دستی" in texts.purchase_open_in_review.format(order_id=9)


@pytest.mark.asyncio
async def test_send_service_card_forces_show_renew() -> None:
    overview = _overview(expire=datetime.now(tz=UTC) + timedelta(days=60))
    settings = MagicMock()
    settings.service_expire_warn_days = 7
    settings.display_timezone = "Asia/Tehran"
    message = AsyncMock()
    with patch("bot.delivery.render_subscription_qr_png", return_value=b"PNG"):
        await send_service_card(
            overview=overview,
            settings=settings,
            message=message,
            intro=texts.renewal_reminder_intro.format(days=3),
            show_renew=True,
        )
    kwargs = message.answer_photo.await_args.kwargs
    assert "به‌زودی منقضی" in kwargs["caption"]
    renew_cbs = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert AccountCB.svc_renew(42) in renew_cbs


@pytest.mark.asyncio
async def test_delivery_is_one_message_with_the_subscription_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overview = _overview(
        expire=datetime.now(tz=UTC) + timedelta(days=60),
        subscription_url=" https://sub.example/AbC?key=x%2By ",
    )
    settings = MagicMock()
    settings.service_expire_warn_days = 7
    settings.display_timezone = "Asia/Tehran"
    message = AsyncMock()

    async def _load(*_a, **_k):
        return overview

    async def _not_a_trial(*_a, **_k):
        return False

    monkeypatch.setattr("bot.delivery.load_overview_for_delivery", _load)
    monkeypatch.setattr("bot.delivery.is_trial_account", _not_a_trial)
    session = AsyncMock()
    session.sync_session = MagicMock()
    session.sync_session.in_transaction.return_value = False
    with patch("bot.delivery.service_qr_png", return_value=b"PNG"):
        ok = await deliver_provisioned_service_card(
            session, settings, user_id=1, account_id=42, message=message, intro="ok"
        )
    assert ok is True
    message.answer_photo.assert_awaited_once()
    message.answer.assert_not_awaited()
    rows = message.answer_photo.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].url == "https://sub.example/AbC?key=x%2By"
    assert rows[0][0].style == "primary"
    assert all(button.callback_data != AccountCB.svc_howto(42) for row in rows for button in row)


def test_wallet_path_skips_activating_when_card_follows() -> None:
    src = Path("bot/handlers/purchase.py").read_text(encoding="utf-8")
    # Activating message only in except/else fallbacks, not before deliver
    idx = src.index("if account is not None:")
    block = src[idx : idx + 800]
    assert "deliver_provisioned_service_card" in block
    # First answer in success path should not be payment_wallet_only_ok before deliver
    before_deliver = block.split("deliver_provisioned_service_card")[0]
    assert "payment_wallet_only_ok" not in before_deliver


def test_admin_wires_provision_failed_keyboard() -> None:
    src = Path("bot/handlers/admin.py").read_text(encoding="utf-8")
    assert "provision_failed_keyboard()" in src


def test_payment_wires_receipt_keyboard() -> None:
    src = Path("bot/handlers/payment.py").read_text(encoding="utf-8")
    assert "receipt_waiting_keyboard()" in src


def test_renewal_job_uses_delivery() -> None:
    src = Path("scheduler/jobs/renewal_reminders.py").read_text(encoding="utf-8")
    assert "deliver_provisioned_service_card" in src
    assert "show_renew=True" in src
    assert "renewal_reminder_intro" in src
    sig = inspect.signature(
        __import__(
            "scheduler.jobs.renewal_reminders", fromlist=["run_renewal_reminders"]
        ).run_renewal_reminders
    )
    assert "settings" in sig.parameters


def test_service_detail_renew_button_visible() -> None:
    kb = service_detail_keyboard(9, has_link=True, show_renew=True)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("تمدید" in t for t in labels)
