"""Report group wiring and the outbox that feeds it.

Reports go to a Telegram forum group, one topic per kind of event, so the
owner can mute «کیف پول» without losing «فروش». Two tables:

``ReportSettings`` — which group, which topic per kind, and which of those
topics has actually been proven to work with a real test send.

``ReportOutbox`` — a report is written here **inside the business transaction**
and delivered afterwards. That ordering is the whole point: a Telegram failure
can never roll back a payment, and a rolled-back payment can never produce a
report claiming it happened.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

REPORT_SETTINGS_ROW_ID = 1

# One mute button per question the owner actually asks. Wallet used to sit
# inside payments, so muting referral noise also muted receipt approvals.
# Telegram only allows the six colours below; backup shares pink with
# «ورود و تست» because a seventh legal colour does not exist.
TOPIC_SALES = "sales"
TOPIC_PAYMENTS = "payments"
TOPIC_WALLET = "wallet"
TOPIC_CUSTOMERS = "customers"
TOPIC_ERRORS = "errors"
TOPIC_SYSTEM = "system"
TOPIC_BACKUP = "backup"
TOPIC_DISRUPTIONS = "disruptions"

TOPIC_KEYS: tuple[str, ...] = (
    TOPIC_SALES,
    TOPIC_PAYMENTS,
    TOPIC_WALLET,
    TOPIC_CUSTOMERS,
    TOPIC_ERRORS,
    TOPIC_SYSTEM,
    TOPIC_BACKUP,
    TOPIC_DISRUPTIONS,
)

TOPIC_TITLES_FA: dict[str, str] = {
    TOPIC_SALES: "🛒 فروش",
    TOPIC_PAYMENTS: "💳 پرداخت",
    TOPIC_WALLET: "🏦 کیف پول",
    TOPIC_CUSTOMERS: "👤 ورود و تست",
    TOPIC_ERRORS: "🚨 پیگیری",
    TOPIC_SYSTEM: "📊 جمع‌بندی",
    TOPIC_BACKUP: "💾 بکاپ",
    TOPIC_DISRUPTIONS: "📡 اختلال",
}

# createForumTopic rejects any colour outside this set.
TOPIC_ICON_COLORS_ALLOWED: frozenset[int] = frozenset(
    {
        0x6FB9F0,  # blue
        0xFFD67E,  # yellow
        0xCB86DB,  # purple
        0x8EEE98,  # green
        0xFF93B2,  # pink
        0xFB6F5F,  # red
    }
)

TOPIC_ICON_COLORS: dict[str, int] = {
    TOPIC_SALES: 0x8EEE98,  # green — money in
    TOPIC_PAYMENTS: 0x6FB9F0,  # blue — a receipt
    TOPIC_WALLET: 0xFFD67E,  # yellow — a balance move
    TOPIC_CUSTOMERS: 0xFF93B2,  # pink — a person showed up
    TOPIC_ERRORS: 0xFB6F5F,  # red — a human has to act
    TOPIC_SYSTEM: 0xCB86DB,  # purple — the day, once
    TOPIC_BACKUP: 0xFF93B2,  # pink again — Telegram has no seventh colour
    TOPIC_DISRUPTIONS: 0xFFD67E,  # yellow — an ISP to look at, not an emergency
}


class OutboxState(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class ReportSettings(Base):
    """Singleton row describing the report group."""

    __tablename__ = "report_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # topic key -> message_thread_id
    topics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # topic key -> ISO timestamp of the last successful test send. A topic that
    # has never been proven is shown as «تست‌نشده» rather than assumed healthy.
    verified: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ReportOutbox(Base):
    """One report waiting to be delivered.

    ``event_key`` identifies the **transition**, not the order: a wallet credit,
    a retry debit and a refund on one order are three different reports, while a
    duplicated call for the same transition is one.
    """

    __tablename__ = "report_outbox"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_report_outbox_event_key"),
        Index("ix_report_outbox_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    topic_key: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Database user id for the «کارت مشتری» button. Null on digests and anything
    # that is not about one person. Not a foreign key: deleting the user must
    # not block a report that already happened.
    link_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[OutboxState] = mapped_column(
        Enum(OutboxState, name="report_outbox_state", create_constraint=True),
        nullable=False,
        default=OutboxState.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
