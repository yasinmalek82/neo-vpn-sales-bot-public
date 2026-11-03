"""What the marketing screen says about each campaign, for the bot and the panel.

The bot's «📣 پیام‌های بازاریابی» and the Mini App page print the same title, state,
description and offer line for a campaign; both ask ``campaign_cards`` so they cannot
disagree. Settings only: nothing here sends a message or promises a customer anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.keyboards.marketing import (
    CAMPAIGN_NAMES,
    offer_hours_label,
    offer_percent_label,
    offer_percent_text,
    toggle_label,
)
from bot.texts import texts
from database.models.marketing import OFFER_CAMPAIGNS
from services.marketing import (
    CART_WINDOW_MINUTES,
    TRIAL_CONVERT_MIN_HOURS,
    UPGRADE_MIN_TIME_LEFT_PERCENT,
    WINBACK_MAX_DAYS,
    WINBACK_MIN_DAYS,
)
from services.marketing_settings import MarketingConfig
from services.ux_format import format_jalali, to_persian_digits


def _fa(n: int) -> str:
    return to_persian_digits(n)


def what_text(campaign: str) -> str:
    return texts.admin_mkt_what[campaign].format(
        cart_minutes=_fa(CART_WINDOW_MINUTES),
        trial_hours=_fa(TRIAL_CONVERT_MIN_HOURS),
        win_from=_fa(WINBACK_MIN_DAYS),
        win_to=_fa(WINBACK_MAX_DAYS),
        time_left=_fa(UPGRADE_MIN_TIME_LEFT_PERCENT),
    )


@dataclass(frozen=True)
class CampaignCard:
    campaign: str
    on: bool
    title: str
    state: str
    what: str
    offer_line: str | None
    can_offer: bool
    percent: int
    hours: int
    percent_label: str
    hours_label: str
    # The panel's twins: the switch says on or off there, so no ✅/❌ in front of a name.
    plain_title: str = ""
    plain_percent_label: str = ""

    @property
    def text(self) -> str:
        """The bot's block for this campaign, exactly as its screen prints it."""
        line = texts.admin_mkt_line.format(title=self.title, state=self.state, what=self.what)
        return line + ("\n" + self.offer_line if self.offer_line else "")


def campaign_cards(config: MarketingConfig) -> list[CampaignCard]:
    cards: list[CampaignCard] = []
    for campaign in CAMPAIGN_NAMES:
        since = config.since(campaign)
        state = (
            texts.admin_mkt_state_on.format(since=format_jalali(since, persian_digits=True))
            if since is not None
            else texts.admin_mkt_state_off
        )
        percent = config.offer_percent.get(campaign, 0)
        hours = config.offer_hours.get(campaign, 0)
        can_offer = campaign in OFFER_CAMPAIGNS
        cards.append(
            CampaignCard(
                campaign=campaign,
                on=since is not None,
                title=toggle_label(campaign, on=since is not None),
                state=state,
                what=what_text(campaign),
                offer_line=(
                    texts.admin_mkt_offer_state.format(percent=_fa(percent), hours=_fa(hours))
                    if percent > 0
                    else None
                ),
                can_offer=can_offer,
                percent=percent,
                hours=hours,
                percent_label=offer_percent_label(campaign, percent) if can_offer else "",
                hours_label=offer_hours_label(campaign, hours) if can_offer else "",
                plain_title=CAMPAIGN_NAMES[campaign],
                plain_percent_label=offer_percent_text(campaign, percent) if can_offer else "",
            )
        )
    return cards


def screen_text(config: MarketingConfig) -> str:
    return texts.admin_mkt_screen.format(
        lines="\n\n".join(card.text for card in campaign_cards(config))
    )
