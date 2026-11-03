"""The owner's numbers for warning customers and deleting ended services.

``LifecycleSettings`` is a singleton row (id = 1), like ``report_settings``.
No row means «the defaults», so a quiet shop never writes it: the env list of
reminder days only seeds the first row the admin screen creates.

``ServiceNotice`` is the idempotency marker of every message the lifecycle jobs
send to a customer. ``event_key`` is unique, so a retry or a second run cannot
claim the same notice twice; ``sent_at`` is stamped BEFORE the send (a crash
loses a notice, it never repeats one). See ``services/service_notices.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

LIFECYCLE_SETTINGS_ROW_ID = 1

DELETE_MODE_OFF = "off"
DELETE_MODE_REHEARSAL = "rehearsal"
DELETE_MODE_ON = "on"
DELETE_MODES: tuple[str, ...] = (DELETE_MODE_OFF, DELETE_MODE_REHEARSAL, DELETE_MODE_ON)

# Defaults the migration and the code agree on (owner, 2026-10-03).
DEFAULT_WARN_DAYS: list[int] = [3, 1]
DEFAULT_WARN_VOLUME_PERCENTS: list[int] = [20, 5]
DEFAULT_DELETE_AFTER_EXPIRY_DAYS = 10
DEFAULT_DELETE_AFTER_LIMITED_DAYS = 10
DEFAULT_DELETE_TRIAL_AFTER_DAYS = 1
DEFAULT_DELETE_WARN_DAYS_BEFORE = 3

_JSON_LIST = JSON().with_variant(JSONB, "postgresql")


class LifecycleSettings(Base):
    """Singleton row: warning days, volume percents, and the deletion rules."""

    __tablename__ = "lifecycle_settings"
    __table_args__ = (
        CheckConstraint("delete_mode IN ('off', 'rehearsal', 'on')", name="ck_lifecycle_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    warn_days: Mapped[list[int]] = mapped_column(
        _JSON_LIST, nullable=False, default=list, server_default=text("'[3, 1]'")
    )
    warn_volume_percents: Mapped[list[int]] = mapped_column(
        _JSON_LIST, nullable=False, default=list, server_default=text("'[20, 5]'")
    )
    delete_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELETE_MODE_OFF, server_default="off"
    )
    delete_after_expiry_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    delete_after_limited_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    delete_trial_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    delete_warn_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    last_rehearsal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


NOTICE_VOLUME_WARNING = "volume_warning"
NOTICE_DELETE_WARNING = "delete_warning"
NOTICE_DELETED = "deleted"
NOTICE_TRIAL_ENDING = "trial_ending"


class ServiceNotice(Base):
    """One message to a customer, claimed once by its ``event_key``."""

    __tablename__ = "service_notices"
    __table_args__ = (
        Index("uq_service_notices_event_key", "event_key", unique=True),
        Index("ix_service_notices_account_id", "account_id"),
        Index(
            "ix_service_notices_unsent",
            "kind",
            sqlite_where=text("sent_at IS NULL"),
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # NULL for a marketing notice that belongs to no live service (the cart reminder).
    account_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("pasarguard_accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The person who gets it: the account's owner (a reseller for their customer).
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    # NULL = queued, not claimed. Set before the send, cleared again only when
    # the send failed in a way a later run may retry.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The send was refused for good (the customer blocked the bot). The claim stays so
    # it is not retried every run, and a later deletion says «هشدار نرسید».
    # Set after the message was really handed to Telegram, or refused for good BY THE
    # RECIPIENT (blocked, deactivated). A claim without it is a crash between claim and
    # send: it does not count as a delivered warning, and the next run sends it again.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undelivered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


BATCH_KIND_STOP = "stop"  # a run hit the hard stop and stored what it would have deleted
BATCH_KIND_MANUAL = "manual"  # the owner built a fresh list from the preview
BATCH_KIND_PREVIEW = "preview"  # «👁 پیش‌نمایش»: shown, never approvable
BATCH_KINDS: tuple[str, ...] = (BATCH_KIND_STOP, BATCH_KIND_MANUAL, BATCH_KIND_PREVIEW)
BATCH_OPEN = "open"
BATCH_USED = "used"
BATCH_VOID = "void"

ITEM_DELETE = "delete"
ITEM_WARN = "warn"


class LifecycleBatch(Base):
    """A list of services the owner approves by hand (or only looks at, for a preview).

    Approval deletes ONLY these ids, each re-read and re-decided right before its
    delete. ``numbers_key`` is the deletion numbers it was made under: a list made
    under other numbers is void, and so is one older than 48 hours.
    """

    __tablename__ = "lifecycle_batches"
    __table_args__ = (
        CheckConstraint("kind IN ('stop', 'manual', 'preview')", name="ck_lifecycle_batch_kind"),
        CheckConstraint("status IN ('open', 'used', 'void')", name="ck_lifecycle_batch_status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default=BATCH_OPEN)
    numbers_key: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LifecycleBatchItem(Base):
    """One service in a batch, with the reason it was listed (code and number of days)."""

    __tablename__ = "lifecycle_batch_items"
    __table_args__ = (
        Index("uq_lifecycle_batch_items_batch_account", "batch_id", "account_id", unique=True),
        CheckConstraint("action IN ('delete', 'warn')", name="ck_lifecycle_item_action"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    batch_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("lifecycle_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("pasarguard_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    # expired | limited | trial; `reason_days` = whole days since it ended.
    reason_code: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True for a paid service already past its deadline whose warning has not been
    # sent: «بعد از هشدار حذف می‌شه».
    after_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
