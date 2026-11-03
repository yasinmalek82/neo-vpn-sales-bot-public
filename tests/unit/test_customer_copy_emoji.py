"""T-034: customer-facing copy carries the agreed emoji set, verbatim.

A snapshot of the wording after the copy pass of 2026-10-03; an edit to one of
these strings is a deliberate change to what customers read, so update it here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.texts.fa import PersianTexts
from services.accounts import AccountOverview
from services.service_card import build_service_card_caption
from tests.unit.test_brand_voice import _count_emoji


def _expiring_soon() -> AccountOverview:
    return AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime.now(UTC) + timedelta(days=2),
        used_traffic=2 * 1024**3,
        data_limit=10 * 1024**3,
        subscription_url="https://panel.example.com/sub/neo_1",
        panel_username="neo_u1",
    )


TOUCHED_EXACT: dict[str, str] = {
    "welcome_new": "سلام {name} 👋\n\nاینجا نئو نتورکه. سرویس می‌گیری و وضعیتش رو همین‌جا می‌بینی.\n👇 از منوی پایین شروع کن.",
    "welcome_back": "خوش برگشتی {name} 👋\n👇 از منوی پایین ادامه بده.",
    "purchase_choose_type": "🛒 یکی از گروه‌ها رو انتخاب کن:",
    "purchase_choose_ready_plan": "📦 یکی از پلن‌ها رو انتخاب کن:",
    "purchase_flat_shelf_intro": "📦 پلن‌ها از کم به زیاد چیده شدن.\nقیمت‌ها به تومانه؛ «گیگی» یعنی هر گیگ چند.",
    "purchase_flat_shelf_intro_unlimited": "📦 پلن‌ها از کم به زیاد چیده شدن.\nقیمت‌ها به تومانه.",
    "purchase_choose_volume": "📦 چند گیگ لازم داری؟",
    "purchase_choose_users": "👥 چند نفر همزمان وصل می‌شن؟",
    "purchase_choose_duration_months": "📅 برای چند ماه؟",
    "purchase_enter_promo": "🎫 کد تخفیف داری؟ همین‌جا بفرست.\n\nاگه کد نداری، «بدون کد تخفیف» رو بزن.",
    "purchase_created": "🧾 سفارش #{order_id} ثبت شد.\n\nبرای تکمیل خرید، پرداخت رو از همین‌جا ادامه بده.",
    "purchase_cancelled": "❌ خرید لغو شد. چیزی از حسابت کم نشد.\nهر وقت خواستی، از «خرید اشتراک» دوباره شروع کن.",
    "payment_need_photo": "📸 رسید رو به‌صورت عکس یا فایل تصویری بفرست.",
    "payment_wallet_only_ok": "🏦 سفارش #{order_id} از کیف پولت پرداخت شد.\nدارم فعالش می‌کنم؛ کارت سرویس همین‌جا میاد.",
    "payment_ready_intro": "🎉 <b>سرویس سفارش #{order_id} آماده‌ست.</b>",
    "payment_rejected_user_reason": "❌ پرداخت سفارش #{order_id} تأیید نشد.\n\nدلیل: {reason}\n\nاگه فکر می‌کنی اشتباهی شده، به پشتیبانی بگو تا بررسی بشه.",
    "payment_order_expired": "⏰ مهلت پرداخت سفارش #{order_id} تموم شد.\nهر وقت خواستی، از «خرید اشتراک» دوباره سفارش بده.",
    "account_services_title": "📡 سرویس‌های تو:\nروی هر کدوم بزن تا جزئیاتش باز شه.",
    "account_link_title": "🔗 لینک سابسکریپشن (برای کپی روش بزن):",
    "wallet_menu": "🏦 کیف پول من\n\n💰 موجودی: <b>{balance}</b> تومان",
    "support": "💬 پشتیبانی\n\nهر چی هست بپرس — اتصال، تمدید، یا سروری که قطع شده.",
    "trial_ready_intro": "🎟 تست رایگان فعال شد ({days} روز / {gb} گیگ)\n📂 گروه: {group}",
    "trial_see_my_services": "🎟 تست رایگانت فعاله.\nلینک و اطلاعات اتصال رو از «سرویس‌های من» ببین.",
}


@pytest.mark.parametrize("attr,expected", TOUCHED_EXACT.items(), ids=list(TOUCHED_EXACT))
def test_touched_string_matches_verbatim(attr: str, expected: str) -> None:
    assert getattr(PersianTexts, attr) == expected


def test_payment_instructions_first_and_last_line() -> None:
    lines = PersianTexts.payment_instructions.splitlines()
    assert lines[0] == "🧾 <b>پرداخت سفارش #{order_id}</b>"
    assert lines[-1] == "📸 بعد از واریز، عکس یا فایل تصویری رسید رو همین‌جا بفرست."


def test_payment_receipt_received_first_line() -> None:
    lines = PersianTexts.payment_receipt_received.splitlines()
    assert lines[0] == "📨 رسید سفارش #{order_id} ثبت شد."
    assert "✅" not in PersianTexts.payment_receipt_received


def test_referral_home_first_line() -> None:
    lines = PersianTexts.referral_home.splitlines()
    assert lines[0] == "🎁 دعوت دوستان"


def test_order_summary_lines_carry_receipt_emoji() -> None:
    assert PersianTexts.purchase_summary_wallet.startswith("🧾 <b>خلاصهٔ سفارش</b>")


def test_composed_payment_and_trial_cards_respect_the_line_rule() -> None:
    overview = _expiring_soon()
    for intro in (
        PersianTexts.payment_ready_intro.format(order_id=12),
        PersianTexts.trial_ready_intro.format(days=1, gb=5, group="حجمی ویژه"),
    ):
        caption = build_service_card_caption(overview, warn_days=7, intro=intro)
        for line in caption.splitlines():
            if line.strip():
                assert _count_emoji(line) <= 1, line


def test_touched_customer_strings_do_not_contain_check_mark() -> None:
    names = set(TOUCHED_EXACT) | {
        "payment_instructions",
        "payment_receipt_received",
        "referral_home",
        "purchase_summary_wallet",
    }
    offenders = [name for name in names if "✅" in getattr(PersianTexts, name)]
    assert not offenders, f"unexpected ✅ in: {offenders}"


def test_composed_budget_goes_red_when_intro_gets_a_second_emoji() -> None:
    """Mutation check for the composed-budget assertion above.

    Adding a second emoji to ``payment_ready_intro`` must push the delivered
    caption over the per-line rule (badge yields on any intro emoji, so the
    two land on the intro line itself).
    """
    overview = _expiring_soon()
    mutated = PersianTexts.payment_ready_intro.format(order_id=12) + " 🎉"
    caption = build_service_card_caption(overview, warn_days=7, intro=mutated)
    intro_line = caption.splitlines()[0]
    assert _count_emoji(intro_line) > 1, caption
