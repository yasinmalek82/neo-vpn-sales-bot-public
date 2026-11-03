"""UX helpers: account keyboards, Persian overview text, menu wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from bot.handlers import menu as menu_mod
from bot.handlers.account import (
    _truncate_caption,
    format_orders_text,
    format_overview_text,
)
from bot.keyboards.account import (
    AccountCB,
    account_empty_keyboard,
    account_keyboard,
    account_orders_keyboard,
    howto_platform_keyboard,
    parse_howto_callback,
    parse_revoke_callback,
    service_detail_keyboard,
    service_revoke_confirm_keyboard,
    services_list_keyboard,
)
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.purchase import PurchaseCB, plan_type_keyboard, renew_or_new_keyboard
from bot.keyboards.referral import ReferralCB, referral_keyboard
from bot.texts import texts
from services.accounts import AccountOverview, OrderSummary, account_label


def test_account_keyboard_includes_refresh_and_optional_link_not_orders() -> None:
    kb = account_keyboard(has_link=True)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert AccountCB.REFRESH in callbacks
    assert AccountCB.ORDERS not in callbacks
    assert AccountCB.LINK in callbacks
    assert AccountCB.QR in callbacks
    assert MenuCallback.BUY in callbacks
    assert AccountCB.BACK in callbacks
    assert any("به‌روزرسانی" in text for text in labels)
    assert any(text.startswith("🔄") for text in labels)
    assert any(text.startswith("🔗") for text in labels)
    assert any(text.startswith("📱") for text in labels)

    kb_no_link = account_keyboard(has_link=False)
    callbacks_no_link = [btn.callback_data for row in kb_no_link.inline_keyboard for btn in row]
    assert AccountCB.LINK not in callbacks_no_link
    assert AccountCB.QR not in callbacks_no_link
    assert AccountCB.ORDERS not in callbacks_no_link


def test_services_list_and_detail_keyboards() -> None:
    services = [
        SimpleNamespace(
            id=7, display_name="Alpha", service_name="alpha", panel_username="yasin_u7jf"
        ),
        SimpleNamespace(id=8, display_name=None, service_name="beta", panel_username="b_2xyz"),
    ]
    list_kb = services_list_keyboard(services)  # type: ignore[arg-type]
    list_cb = [btn.callback_data for row in list_kb.inline_keyboard for btn in row]
    list_labels = [btn.text for row in list_kb.inline_keyboard for btn in row]
    assert AccountCB.svc(7) in list_cb
    assert AccountCB.svc(8) in list_cb
    assert any("yasin_u7jf" in text for text in list_labels)
    assert any("b_2xyz" in text for text in list_labels)
    assert any(
        text.startswith("🟢") or text.startswith("🟡") or text.startswith("🔴")
        for text in list_labels
    )
    assert "Alpha" not in list_labels
    assert "beta" not in list_labels
    assert MenuCallback.BUY in list_cb
    assert AccountCB.BACK in list_cb
    assert any("خرید اشتراک" in text for text in list_labels)
    assert any(text.startswith("🛒") for text in list_labels)
    assert any(text.startswith("⬅️") for text in list_labels)

    detail = service_detail_keyboard(7, has_link=True)
    detail_cb = [btn.callback_data for row in detail.inline_keyboard for btn in row]
    detail_labels = [btn.text for row in detail.inline_keyboard for btn in row]
    assert AccountCB.svc_refresh(7) in detail_cb
    assert AccountCB.svc_howto(7) in detail_cb
    assert AccountCB.svc_revoke(7) in detail_cb
    assert AccountCB.svc_link(7) not in detail_cb
    assert AccountCB.svc_qr(7) not in detail_cb
    assert AccountCB.ORDERS not in detail_cb
    assert AccountCB.LIST in detail_cb
    assert AccountCB.BACK not in detail_cb
    assert any("راهنمای اتصال" in text for text in detail_labels)
    assert any("تغییر لینک" in text for text in detail_labels)
    assert not any("نمایش لینک" in text for text in detail_labels)
    assert not any("QR" in text for text in detail_labels)

    detail_no_link = service_detail_keyboard(7, has_link=False)
    detail_no_link_cb = [btn.callback_data for row in detail_no_link.inline_keyboard for btn in row]
    assert AccountCB.svc_howto(7) not in detail_no_link_cb
    assert AccountCB.svc_revoke(7) not in detail_no_link_cb
    assert AccountCB.svc_link(7) not in detail_no_link_cb
    assert AccountCB.svc_qr(7) not in detail_no_link_cb
    assert AccountCB.ORDERS not in detail_no_link_cb

    detail_renew = service_detail_keyboard(7, has_link=True, show_renew=True)
    renew_cb = [btn.callback_data for row in detail_renew.inline_keyboard for btn in row]
    renew_labels = [btn.text for row in detail_renew.inline_keyboard for btn in row]
    assert AccountCB.svc_renew(7) in renew_cb
    assert any("تمدید این سرویس" in text for text in renew_labels)


def test_empty_and_orders_keyboards() -> None:
    empty = account_empty_keyboard()
    empty_cb = [btn.callback_data for row in empty.inline_keyboard for btn in row]
    empty_labels = [btn.text for row in empty.inline_keyboard for btn in row]
    assert MenuCallback.BUY in empty_cb
    assert AccountCB.BACK in empty_cb
    assert any(text.startswith("🛒") for text in empty_labels)
    assert any(text.startswith("⬅️") for text in empty_labels)

    empty_trial = account_empty_keyboard(trial_eligible=True)
    trial_labels = [btn.text for row in empty_trial.inline_keyboard for btn in row]
    assert any("تست رایگان" in text for text in trial_labels)
    assert any(text.startswith("🎟") for text in trial_labels)

    orders_kb = account_orders_keyboard()
    orders_cb = [btn.callback_data for row in orders_kb.inline_keyboard for btn in row]
    assert AccountCB.LIST in orders_cb


def test_menu_placeholder_no_longer_owns_account() -> None:
    """menu:account / menu:admin / menu:referral must not be stubs in menu router."""
    source = Path(menu_mod.__file__).read_text(encoding="utf-8")
    assert "MenuCallback.ACCOUNT" not in source
    assert "MenuCallback.ADMIN" not in source
    assert "MenuCallback.REFERRAL" not in source


def test_overview_text_persian_and_stale_flag() -> None:
    overview = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime(2026, 10, 1, 15, 30, tzinfo=UTC),
        used_traffic=2 * 1024**3,
        data_limit=10 * 1024**3,
        subscription_url="https://panel.example.com/sub/neo_1",
        panel_username="yasin_u7jf",
        display_name="shortname",
    )
    text = format_overview_text(overview, warn_days=7, timezone="Asia/Tehran")
    assert "فعال" in text
    assert "سرویس شما" in text or "سرویس:" in text
    assert "۲ گیگ" in text
    assert "۱۰ گیگ" in text
    assert "مصرف حجم" in text or "▓" in text
    assert "https://panel.example.com/sub/neo_1" in text
    assert text.count("https://panel.example.com/sub/neo_1") == 1
    assert "yasin_u7jf" in text
    assert "shortname" not in text
    assert texts.account_stale not in text
    assert "v2rayNG" not in text
    assert "Hiddify" not in text
    assert "UTC" not in text
    # Jalali date present (YYYY/MM/DD HH:MM)
    assert "/" in text
    from services.ux_format import format_jalali

    assert format_jalali(overview.expire, timezone="Asia/Tehran", persian_digits=True) in text

    stale = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=True,
        status="ACTIVE",
        expire=datetime(2026, 10, 1, tzinfo=UTC),
        used_traffic=None,
        data_limit=10 * 1024**3,
        subscription_url=None,
    )
    stale_text = format_overview_text(stale)
    assert texts.account_stale in stale_text
    assert texts.account_no_url in stale_text
    assert texts.account_unknown in stale_text


def test_orders_text() -> None:
    empty = AccountOverview(has_account=False, has_orders=False, stale=False)
    assert format_orders_text(empty) == texts.account_orders_empty

    overview = AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        recent_orders=[
            OrderSummary(
                order_id=7,
                status="ACTIVE",
                price_toman=150_000,
                created_at=datetime.now(UTC),
            )
        ],
    )
    text = format_orders_text(overview)
    assert "#۷" in text
    assert "فعال" in text
    assert "۱۵۰٬۰۰۰" in text or "150" in text


def test_services_title_mentions_details() -> None:
    assert "جزئیات" in texts.account_services_title


def test_persian_strings_exist() -> None:
    for name in (
        "account_services_title",
        "account_services_empty",
        "account_overview",
        "account_stale",
        "account_no_url",
        "account_refreshed",
        "account_orders_title",
        "account_link_title",
        "account_qr_caption",
        "account_qr_error",
        "account_expire_badge",
        "account_howto_pick_platform",
        "account_howto_android",
        "account_howto_ios",
        "account_howto_windows",
        "account_howto_macos",
        "account_revoke_warn",
        "account_revoke_success",
        "account_revoke_fail",
    ):
        assert getattr(texts, name)


def test_account_label_uses_panel_username() -> None:
    named = SimpleNamespace(
        panel_username="yasin_u7jf",
        display_name="myvpn",
        service_name="myvpn",
    )
    assert account_label(named) == "yasin_u7jf"  # type: ignore[arg-type]

    fallback = SimpleNamespace(
        panel_username="",
        display_name="shown",
        service_name="svc",
    )
    assert account_label(fallback) == "shown"  # type: ignore[arg-type]


def test_purchase_and_referral_keyboards_have_emojis() -> None:
    plan_labels = [btn.text for row in plan_type_keyboard().inline_keyboard for btn in row]
    assert any("پلن حجمی" in text and text.startswith("📦") for text in plan_labels)
    assert any("انصراف" in text and text.startswith("❌") for text in plan_labels)

    renew_labels = [btn.text for row in renew_or_new_keyboard().inline_keyboard for btn in row]
    assert any("تمدید" in text and text.startswith("📅") for text in renew_labels)
    assert PurchaseCB.RENEW_SAME in [
        btn.callback_data for row in renew_or_new_keyboard().inline_keyboard for btn in row
    ]

    ref_labels = [
        btn.text for row in referral_keyboard(trial_eligible=True).inline_keyboard for btn in row
    ]
    assert any("تست رایگان" in text and text.startswith("🎟") for text in ref_labels)
    assert any("بازگشت" in text and text.startswith("⬅️") for text in ref_labels)
    ref_cb = [
        btn.callback_data
        for row in referral_keyboard(trial_eligible=True).inline_keyboard
        for btn in row
    ]
    assert ReferralCB.TRIAL in ref_cb


def test_services_list_status_emoji_prefix() -> None:
    from datetime import UTC, datetime, timedelta

    services = [
        SimpleNamespace(id=1, display_name="A", service_name="a", panel_username="ok_user"),
        SimpleNamespace(id=2, display_name="B", service_name="b", panel_username="soon_user"),
        SimpleNamespace(id=3, display_name="C", service_name="c", panel_username="dead_user"),
    ]
    # Relative to real now: the keyboard compares against the current clock,
    # so a hardcoded date silently rots into an "expired" case.
    now = datetime.now(UTC)
    status_by_id = {
        1: ("active", now + timedelta(days=30)),
        2: ("active", now + timedelta(days=2)),
        3: ("disabled", now + timedelta(days=30)),
    }
    kb = services_list_keyboard(services, status_by_id=status_by_id, warn_days=7)  # type: ignore[arg-type]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any(text.startswith("🟢 ok_user") for text in labels)
    assert any(text.startswith("🟡 soon_user") for text in labels)
    assert any(text.startswith("🔴 dead_user") for text in labels)


def test_truncate_caption_respects_telegram_limit() -> None:
    short = "ok"
    assert _truncate_caption(short) == short
    long = "آ" * 1100
    truncated = _truncate_caption(long, limit=1024)
    assert len(truncated) == 1024
    assert truncated.endswith("…")


def test_howto_callback_constant() -> None:
    assert AccountCB.svc_howto(42) == "acct:svc:howto:42"
    assert AccountCB.svc_howto(42, "android") == "acct:svc:howto:42:android"
    assert AccountCB.SVC_HOWTO_PREFIX.startswith(AccountCB.SVC_PREFIX)
    assert parse_howto_callback("acct:svc:howto:42") == (42, None)
    assert parse_howto_callback("acct:svc:howto:42:ios") == (42, "ios")
    assert parse_howto_callback("acct:svc:howto:x") is None
    assert parse_howto_callback("acct:svc:howto:42:linux") is None


def test_howto_platform_keyboard() -> None:
    kb = howto_platform_keyboard(9)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert AccountCB.svc_howto(9, "android") in callbacks
    assert AccountCB.svc_howto(9, "ios") in callbacks
    assert AccountCB.svc_howto(9, "windows") in callbacks
    assert AccountCB.svc_howto(9, "macos") in callbacks
    assert AccountCB.svc(9) in callbacks
    assert any("Android" in text for text in labels)
    assert any("iOS" in text for text in labels)


def test_revoke_callbacks_and_confirm_keyboard() -> None:
    assert AccountCB.svc_revoke(7) == "acct:svc:revoke:7"
    assert AccountCB.svc_revoke_confirm(7) == "acct:svc:revoke:7:confirm"
    assert parse_revoke_callback("acct:svc:revoke:7") == (7, False)
    assert parse_revoke_callback("acct:svc:revoke:7:confirm") == (7, True)
    assert parse_revoke_callback("acct:svc:revoke:x") is None
    kb = service_revoke_confirm_keyboard(7)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert AccountCB.svc_revoke_confirm(7) in callbacks
    assert AccountCB.svc(7) in callbacks
    assert any("لینک جدید" in text for text in labels)


def test_howto_apps_preferred_and_rtl() -> None:
    from bot.texts import texts
    from services.ux_format import as_rtl_lines

    for attr in (
        "account_howto_android",
        "account_howto_ios",
        "account_howto_windows",
        "account_howto_macos",
    ):
        body = getattr(texts, attr)
        assert "Hiddify" not in body
        assert "Happ" in body
        assert "V2Box" in body
        assert "Streisand" in body
        rtl = as_rtl_lines(body)
        assert chr(0x200F) in rtl
        # source strings stay clean; RLM applied at send-time only
        assert chr(0x200F) not in body
