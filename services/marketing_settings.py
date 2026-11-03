"""The owner's switches for marketing nudges.

Read by the jobs (``services/marketing_run.py``, ``services/volume_warnings.py``)
and edited from «⚙️ تنظیمات» → «📣 پیام‌های بازاریابی».

* **No row means every campaign is off, and reading never writes.** An idle job
  must not commit (``tests/integration/test_idle_jobs_read_only.py``); the row is
  created by the first save from the screen.
* **Offer percent and hours** (``trial_convert``, ``winback``, ``upgrade``; never the
  cart) are typed on the same screen and checked here. Percent 0 means no offer;
  the hours only matter once a percent is set.
* **Switching on stores the moment.** A campaign acts only on events after it
  (``enabled_at``); switching off and on again starts a new moment. Switching on
  a campaign that is already on keeps its moment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot.texts import texts
from database.models.marketing import (
    CAMPAIGNS,
    DEFAULT_OFFER_HOURS,
    MARKETING_SETTINGS_ROW_ID,
    MAX_OFFER_HOURS,
    MAX_OFFER_PERCENT,
    OFFER_CAMPAIGNS,
    MarketingSettings,
)
from services.user_offers import OfferSpec
from services.ux_format import to_persian_digits


class MarketingSettingsError(ValueError):
    """A refused change. The message is admin copy, safe to show as it is."""


@dataclass(frozen=True)
class MarketingConfig:
    """What the jobs read. Plain data: no ORM row crosses a network await."""

    enabled_at: dict[str, datetime | None]
    offer_percent: dict[str, int] = field(default_factory=dict)
    offer_hours: dict[str, int] = field(default_factory=dict)

    def offer(self, campaign: str, *, min_volume_gb: int | None = None) -> OfferSpec | None:
        """The personal offer this campaign's message carries, or None when its percent is 0."""
        percent = self.offer_percent.get(campaign, 0)
        if campaign not in OFFER_CAMPAIGNS or percent <= 0:
            return None
        return OfferSpec(
            campaign=campaign,
            percent=percent,
            hours=self.offer_hours.get(campaign, DEFAULT_OFFER_HOURS),
            min_volume_gb=min_volume_gb,
        )

    def is_on(self, campaign: str) -> bool:
        return self.enabled_at.get(campaign) is not None

    def since(self, campaign: str) -> datetime | None:
        return self.enabled_at.get(campaign)

    @property
    def any_on(self) -> bool:
        return any(moment is not None for moment in self.enabled_at.values())


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _field(campaign: str) -> str:
    if campaign not in CAMPAIGNS:
        raise MarketingSettingsError("این پیام وجود ندارد.")
    return f"{campaign}_enabled_at"


def empty_config() -> MarketingConfig:
    return MarketingConfig(enabled_at=dict.fromkeys(CAMPAIGNS))


async def load_config(session: AsyncSession) -> MarketingConfig:
    """The current switches. Never writes: a missing row is everything off."""
    return config_from_row(await session.get(MarketingSettings, MARKETING_SETTINGS_ROW_ID))


def config_from_row(row: MarketingSettings | None) -> MarketingConfig:
    """The switches of a row already read (``None`` is everything off)."""
    if row is None:
        return empty_config()
    return MarketingConfig(
        enabled_at={name: _aware(getattr(row, f"{name}_enabled_at")) for name in CAMPAIGNS},
        offer_percent={
            name: int(getattr(row, f"{name}_offer_percent")) for name in OFFER_CAMPAIGNS
        },
        offer_hours={name: int(getattr(row, f"{name}_offer_hours")) for name in OFFER_CAMPAIGNS},
    )


async def set_campaign(
    session: AsyncSession, *, campaign: str, on: bool, now: datetime | None = None
) -> None:
    """Switch one campaign. The caller commits."""
    field = _field(campaign)
    row = await session.get(MarketingSettings, MARKETING_SETTINGS_ROW_ID)
    if row is None:
        row = MarketingSettings(id=MARKETING_SETTINGS_ROW_ID)
        session.add(row)
    if on:
        if getattr(row, field) is None:
            setattr(row, field, now or datetime.now(UTC))
    else:
        setattr(row, field, None)
    await session.flush()


# --- the personal offer's two numbers ----------------------------------------

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUMBER = re.compile(r"\d{1,6}")


def _typed_number(raw: str) -> int | None:
    text = (raw or "").translate(_DIGITS).strip()
    return int(text) if _NUMBER.fullmatch(text) else None


def parse_offer_percent(raw: str) -> int:
    """The percent typed on the screen: a whole number, 0 to 50 (0 = no offer)."""
    value = _typed_number(raw)
    if value is None or value > MAX_OFFER_PERCENT:
        raise MarketingSettingsError(
            texts.admin_mkt_offer_percent_range.format(high=_fa(MAX_OFFER_PERCENT))
        )
    return value


def parse_offer_hours(raw: str) -> int:
    """The hours typed on the screen: a whole number, 1 to 168."""
    value = _typed_number(raw)
    if value is None or not 1 <= value <= MAX_OFFER_HOURS:
        raise MarketingSettingsError(
            texts.admin_mkt_offer_hours_range.format(high=_fa(MAX_OFFER_HOURS))
        )
    return value


def _fa(n: int) -> str:
    return to_persian_digits(n)


async def _row(session: AsyncSession) -> MarketingSettings:
    row = await session.get(MarketingSettings, MARKETING_SETTINGS_ROW_ID)
    if row is None:
        row = MarketingSettings(id=MARKETING_SETTINGS_ROW_ID)
        session.add(row)
    return row


def _offer_campaign(campaign: str) -> str:
    if campaign not in OFFER_CAMPAIGNS:
        raise MarketingSettingsError(texts.admin_mkt_offer_no_such_campaign)
    return campaign


async def set_offer_percent(session: AsyncSession, *, campaign: str, percent: int) -> None:
    """Store a campaign's offer percent. Re-checks the range, so a forged value cannot
    get past the screen. The caller commits."""
    name = _offer_campaign(campaign)
    if (
        isinstance(percent, bool)
        or not isinstance(percent, int)
        or not 0 <= percent <= MAX_OFFER_PERCENT
    ):
        raise MarketingSettingsError(
            texts.admin_mkt_offer_percent_range.format(high=_fa(MAX_OFFER_PERCENT))
        )
    setattr(await _row(session), f"{name}_offer_percent", percent)
    await session.flush()


async def set_offer_hours(session: AsyncSession, *, campaign: str, hours: int) -> None:
    """Store how long a campaign's offer lasts. The caller commits."""
    name = _offer_campaign(campaign)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_OFFER_HOURS:
        raise MarketingSettingsError(
            texts.admin_mkt_offer_hours_range.format(high=_fa(MAX_OFFER_HOURS))
        )
    setattr(await _row(session), f"{name}_offer_hours", hours)
    await session.flush()
