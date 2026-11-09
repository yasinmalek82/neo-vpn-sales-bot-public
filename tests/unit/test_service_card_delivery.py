"""Service card delivery: caption builder + call-site wiring (no plain URL-only)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.delivery import send_service_card
from bot.texts import texts
from services.accounts import AccountOverview
from services.service_card import build_service_card_caption, format_overview_text, truncate_caption


def _overview(**kwargs) -> AccountOverview:
    base = {
        "has_account": True,
        "has_orders": True,
        "stale": False,
        "account_id": 42,
        "panel_username": "neo_u42ab",
        "display_name": "MySvc",
        "status": "active",
        "expire": datetime.now(tz=UTC) + timedelta(days=20),
        "used_traffic": 1_073_741_824,
        "data_limit": 10_737_418_240,
        "duration_days": 30,
        "subscription_url": "https://sub.example/x",
    }
    base.update(kwargs)
    return AccountOverview(**base)


def test_build_caption_includes_intro_bars_and_truncates() -> None:
    overview = _overview()
    caption = build_service_card_caption(
        overview,
        warn_days=7,
        timezone="Asia/Tehran",
        intro=texts.payment_ready_intro.format(order_id=99),
    )
    assert "سرویس سفارش #99 آماده‌ست." in caption
    assert "neo_u42ab" in caption
    assert "مصرف حجم" in caption or "▓" in caption or "░" in caption
    assert "https://sub.example/x" in caption
    assert len(caption) <= 1024

    long_intro = "آ" * 900
    capped = build_service_card_caption(overview, intro=long_intro)
    assert len(capped) <= 1024
    assert capped.endswith("…") or len(capped) < len(long_intro) + 50


def test_format_overview_reexport_matches_service_card() -> None:
    # account handler re-exports via services.service_card
    from bot.handlers import account as account_mod

    overview = _overview(stale=True)
    assert format_overview_text(overview) == account_mod.format_overview_text(overview)
    assert truncate_caption("x" * 2000, limit=100) == account_mod._truncate_caption(
        "x" * 2000, limit=100
    )


@pytest.mark.asyncio
async def test_send_service_card_photo_with_keyboard_and_text_fallback() -> None:
    overview = _overview()
    settings = MagicMock()
    settings.service_expire_warn_days = 7
    settings.display_timezone = "Asia/Tehran"

    message = AsyncMock()
    with patch("bot.delivery.render_subscription_qr_png", return_value=b"PNGDATA") as render:
        await send_service_card(
            overview=overview,
            settings=settings,
            message=message,
            intro=texts.payment_ready_intro.format(order_id=7),
        )
    render.assert_called_once()
    message.answer_photo.assert_awaited_once()
    kwargs = message.answer_photo.await_args.kwargs
    assert "سرویس سفارش #7 آماده‌ست." in kwargs["caption"]
    assert kwargs["reply_markup"] is not None
    callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
    assert kwargs["reply_markup"].inline_keyboard[0][0].url == "https://sub.example/x"
    assert any(cb and "revoke" in cb for cb in callbacks)

    # QR failure → text fallback
    message2 = AsyncMock()
    with patch("bot.delivery.render_subscription_qr_png", side_effect=RuntimeError("qr boom")):
        await send_service_card(overview=overview, settings=settings, message=message2)
    message2.answer.assert_awaited_once()
    message2.answer_photo.assert_not_awaited()
    fallback = message2.answer.await_args.kwargs["reply_markup"]
    assert fallback.inline_keyboard[0][0].url == "https://sub.example/x"

    # bot.send_photo path
    bot = AsyncMock()
    with patch("bot.delivery.render_subscription_qr_png", return_value=b"PNG"):
        await send_service_card(
            overview=overview, settings=settings, bot=bot, chat_id=12345, intro="hi"
        )
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345


def test_admin_purchase_referral_no_longer_format_payment_approved_with_sub_url_only() -> None:
    """Primary delivery paths must not format payment_approved_user with only sub_url."""
    root = Path(__file__).resolve().parents[2]
    files = [
        root / "bot/handlers/admin.py",
        root / "bot/handlers/purchase.py",
        root / "bot/handlers/referral.py",
    ]
    for path in files:
        src = path.read_text(encoding="utf-8")
        assert "deliver_provisioned_service_card" in src, path.name
        assert "payment_approved_user.format" not in src, path.name
        if path.name == "referral.py":
            # Live path uses trial_ready_intro; legacy strings only in except fallback.
            assert "trial_ready_intro" in src
            assert "trial_see_my_services" in src


def test_payment_ready_intro_exists() -> None:
    assert "آماده‌ست" in texts.payment_ready_intro.format(order_id=1)
    assert "تست رایگان" in texts.trial_ready_intro.format(days=1, gb=1, group="حجمی ویژه")


def test_deliver_helper_signature_stable() -> None:
    from bot.delivery import deliver_provisioned_service_card

    sig = inspect.signature(deliver_provisioned_service_card)
    for name in (
        "user_id",
        "account_id",
        "intro",
        "message",
        "bot",
        "chat_id",
        "show_renew",
    ):
        assert name in sig.parameters
