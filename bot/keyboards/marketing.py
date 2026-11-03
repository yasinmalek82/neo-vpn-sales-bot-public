"""«📣 پیام‌های بازاریابی» keyboard (admin).

One toggle per campaign, named by its mark: «✅ …» on, «❌ …» off
(``.claude/rules/store-admin-ui.md``). Tapping flips it and the screen redraws.
Colour goes through ``styled()``; nothing here is the screen's one primary action.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.account import AccountCB
from bot.keyboards.main_menu import MenuCallback
from bot.keyboards.nav import cancel_button, nav_rows
from bot.keyboards.purchase import PurchaseCB
from bot.keyboards.style import PRIMARY, styled
from database.models.marketing import (
    CAMPAIGN_CART,
    CAMPAIGN_TRIAL_CONVERT,
    CAMPAIGN_UPGRADE,
    CAMPAIGN_WINBACK,
    OFFER_CAMPAIGNS,
)
from services.marketing_settings import MarketingConfig
from services.ux_format import to_persian_digits


class MktCB:
    OPEN = "adm:mk"
    TOGGLE = "adm:mk:t:"  # + campaign code
    PERCENT = "adm:mk:p:"  # + campaign code: type the personal offer's percent
    HOURS = "adm:mk:h:"  # + campaign code: type how long it lasts


# Short codes keep callback_data far under Telegram's 64 bytes.
CAMPAIGN_CODES: dict[str, str] = {
    "c": CAMPAIGN_CART,
    "t": CAMPAIGN_TRIAL_CONVERT,
    "w": CAMPAIGN_WINBACK,
    "u": CAMPAIGN_UPGRADE,
}
CODE_OF_CAMPAIGN: dict[str, str] = {v: k for k, v in CAMPAIGN_CODES.items()}

CAMPAIGN_NAMES: dict[str, str] = {
    CAMPAIGN_CART: "🛒 سفارش نیمه‌کاره",
    CAMPAIGN_TRIAL_CONVERT: "🎁 بعد از تست",
    CAMPAIGN_WINBACK: "👋 بعد از پایان اشتراک",
    CAMPAIGN_UPGRADE: "⚠️ حجم زودتر از زمان",
}


# What each campaign's offer buttons call it (the cart reminder has none).
OFFER_NAMES: dict[str, str] = {
    CAMPAIGN_TRIAL_CONVERT: "بعد از تست",
    CAMPAIGN_WINBACK: "بعد از پایان اشتراک",
    CAMPAIGN_UPGRADE: "حجم زودتر از زمان",
}


def toggle_label(campaign: str, *, on: bool) -> str:
    return f"{'✅' if on else '❌'} {CAMPAIGN_NAMES[campaign]}"


def offer_percent_text(campaign: str, percent: int) -> str:
    """The offer's name with its figure, no state mark (the panel's field label)."""
    if percent <= 0:
        return f"تخفیف {OFFER_NAMES[campaign]}"
    return f"تخفیف {OFFER_NAMES[campaign]}: {to_persian_digits(percent)}٪"


def offer_percent_label(campaign: str, percent: int) -> str:
    """State first: «❌» while there is no offer, «✅ … ۱۵٪» with one."""
    return f"{'✅' if percent > 0 else '❌'} {offer_percent_text(campaign, percent)}"


def offer_hours_label(campaign: str, hours: int) -> str:
    return f"⏳ مهلت تخفیف {OFFER_NAMES[campaign]}: {to_persian_digits(hours)} ساعت"


def marketing_keyboard(config: MarketingConfig, *, can_edit: bool = True) -> InlineKeyboardMarkup:
    """Another admin sees the screen; only the owner gets the toggles."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_edit:
        buttons = [
            InlineKeyboardButton(
                text=toggle_label(name, on=config.is_on(name)),
                callback_data=f"{MktCB.TOGGLE}{CODE_OF_CAMPAIGN[name]}",
            )
            for name in CAMPAIGN_NAMES
        ]
        rows.extend(buttons[i : i + 2] for i in range(0, len(buttons), 2))
        for name in OFFER_CAMPAIGNS:
            percent = config.offer_percent.get(name, 0)
            offer_row = [
                InlineKeyboardButton(
                    text=offer_percent_label(name, percent),
                    callback_data=f"{MktCB.PERCENT}{CODE_OF_CAMPAIGN[name]}",
                )
            ]
            # Hide until relevant: the hours time out an offer, so they matter only with one.
            if percent > 0:
                offer_row.append(
                    InlineKeyboardButton(
                        text=offer_hours_label(name, config.offer_hours.get(name, 0)),
                        callback_data=f"{MktCB.HOURS}{CODE_OF_CAMPAIGN[name]}",
                    )
                )
            rows.append(offer_row)
    rows.extend(nav_rows(back_to="adm:hx:set", back_label="تنظیمات", home=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def marketing_input_keyboard() -> InlineKeyboardMarkup:
    """Under a typed-number prompt: a way out, and a way back to the screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [cancel_button()],
            *nav_rows(back_to=MktCB.OPEN, back_label="پیام‌های بازاریابی", home=True),
        ]
    )


def _one_button(text: str) -> InlineKeyboardMarkup:
    """Under a nudge: one way on, through the buy menu (it resumes an open order first)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, style=styled(PRIMARY), callback_data=MenuCallback.BUY)]
        ]
    )


def cart_keyboard() -> InlineKeyboardMarkup:
    return _one_button("ادامهٔ سفارش")


def trial_convert_keyboard() -> InlineKeyboardMarkup:
    return _one_button("خرید اشتراک")


def winback_keyboard() -> InlineKeyboardMarkup:
    return _one_button("خرید یا تمدید")


def volume_upgrade_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Under the upgrade wording of the volume warning: the plan switch for this service
    (the screen's one action), and the plain renewal."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="تغییر پلن",
                    style=styled(PRIMARY),
                    callback_data=f"{PurchaseCB.SWITCH_PREFIX}{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="تمدید همین سرویس", callback_data=AccountCB.svc_renew(account_id)
                )
            ],
        ]
    )
