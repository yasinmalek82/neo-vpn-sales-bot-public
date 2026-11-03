"""The owner's switches for marketing nudges to retail customers.

``MarketingSettings`` is a singleton row (id = 1), shaped like
``LifecycleSettings``: no row means every campaign is off, and a quiet shop
never writes it. Per campaign ``<name>_enabled_at`` is NULL while off and the
moment it was switched on otherwise: a campaign never acts on something that
happened before that moment, and switching off and on again resets it.

``<name>_offer_percent`` and ``<name>_offer_hours`` are the personal offer a
campaign may carry (``services/user_offers.py``): percent 0 means none, and the
hours are how long it lasts from the send. They exist from the first revision so
the table did not change twice.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

MARKETING_SETTINGS_ROW_ID = 1

CAMPAIGN_CART = "cart"
CAMPAIGN_TRIAL_CONVERT = "trial_convert"
CAMPAIGN_WINBACK = "winback"
CAMPAIGN_UPGRADE = "upgrade"
CAMPAIGNS: tuple[str, ...] = (
    CAMPAIGN_CART,
    CAMPAIGN_TRIAL_CONVERT,
    CAMPAIGN_WINBACK,
    CAMPAIGN_UPGRADE,
)

# The campaigns that may carry a personal offer (never the cart reminder).
OFFER_CAMPAIGNS: tuple[str, ...] = (CAMPAIGN_TRIAL_CONVERT, CAMPAIGN_WINBACK, CAMPAIGN_UPGRADE)

# ``service_notices.kind`` of each campaign's message. The upgrade wording is not
# a notice of its own: it goes out as the lifecycle volume warning.
NOTICE_MKT_CART = "mkt_cart"
NOTICE_MKT_TRIAL_CONVERT = "mkt_trial"
NOTICE_MKT_WINBACK = "mkt_winback"
# Kinds that count toward the «one nudge per user per 24 hours» cap. The cart
# reminder is a reminder about an order the customer started: it is exempt.
CAPPED_NOTICE_KINDS: tuple[str, ...] = (NOTICE_MKT_TRIAL_CONVERT, NOTICE_MKT_WINBACK)

DEFAULT_OFFER_HOURS = 48
MAX_OFFER_PERCENT = 50
MAX_OFFER_HOURS = 168


class MarketingSettings(Base):
    """Singleton row: when each campaign was switched on (NULL = off)."""

    __tablename__ = "marketing_settings"
    __table_args__ = (
        CheckConstraint(
            "trial_convert_offer_percent BETWEEN 0 AND 50"
            " AND winback_offer_percent BETWEEN 0 AND 50"
            " AND upgrade_offer_percent BETWEEN 0 AND 50",
            name="ck_marketing_offer_percent",
        ),
        CheckConstraint(
            "trial_convert_offer_hours BETWEEN 1 AND 168"
            " AND winback_offer_hours BETWEEN 1 AND 168"
            " AND upgrade_offer_hours BETWEEN 1 AND 168",
            name="ck_marketing_offer_hours",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    cart_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_convert_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    winback_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upgrade_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_convert_offer_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    trial_convert_offer_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_OFFER_HOURS, server_default="48"
    )
    winback_offer_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    winback_offer_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_OFFER_HOURS, server_default="48"
    )
    upgrade_offer_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    upgrade_offer_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_OFFER_HOURS, server_default="48"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
