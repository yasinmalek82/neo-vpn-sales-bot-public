from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.payment import Payment
    from database.models.plan import Plan
    from database.models.referral_log import ReferralLog
    from database.models.user import User


class OrderStatus(str, enum.Enum):
    """Product order lifecycle (aligned with PROJECT_SPEC).

    Legacy scaffold values (pending/paid/activated/expired/cancelled/refunded)
    are replaced by this enum in Phase 1. See PROJECT_STATE.md for mapping.
    """

    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAYMENT_REVIEW = "PAYMENT_REVIEW"
    PAYMENT_APPROVED = "PAYMENT_APPROVED"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    PROVISION_FAILED = "PROVISION_FAILED"
    EXPIRED = "EXPIRED"


# An order that will never be paid or delivered: whatever was held against it
# (a personal offer, a promo use) is free again. ``PROVISION_FAILED`` is NOT
# here: the money was taken and the admin retries, so it still holds its claim.
# One set, used by checkout and the order writer; never inline the list.
DEAD_ORDER_STATUSES = frozenset({OrderStatus.REJECTED, OrderStatus.EXPIRED})

TRIAL_KIND_PERSONAL = "personal"
TRIAL_KIND_RESELLER = "reseller"

# `orders.renew_mode`. NULL = an ordinary extension (and every legacy row).
RENEW_MODE_RESET = "reset"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_panel_username", "panel_username", unique=False),
        Index("ix_orders_created_at", "created_at"),
        CheckConstraint(
            "trial_kind IS NULL OR trial_kind IN ('personal', 'reseller')",
            name="ck_orders_trial_kind",
        ),
        # An admin-created service carries the request key its confirm button
        # was minted with; a replay looks the order up by it. NULL for every
        # other order, and NULLs do not collide in a unique index.
        Index("uq_orders_admin_request_key", "admin_request_key", unique=True),
        Index(
            "uq_orders_personal_trial_user_group",
            "user_id",
            "trial_category_slug",
            unique=True,
            sqlite_where=text("trial_kind = 'personal' AND trial_voided_at IS NULL"),
            postgresql_where=text("trial_kind = 'personal' AND trial_voided_at IS NULL"),
        ),
        # One open checkout per user. Historical duplicates are not deleted;
        # the migration refuses to build this index while any remain.
        Index(
            "uq_orders_one_open_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW')"),
            postgresql_where=text(
                "status IN ('PENDING_PAYMENT'::order_status, 'PAYMENT_REVIEW'::order_status)"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", create_constraint=True),
        default=OrderStatus.PENDING_PAYMENT,
        nullable=False,
    )
    price_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_toman: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    wallet_applied_toman: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Explicit durable flag (replaces panel_password == "__force_new__" sentinel).
    force_new: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # With ``renew_account_id``: replace that account's plan (expiry from today,
    # fresh volume, new groups and device limit) instead of extending it. Only
    # ever set by the customer choosing another plan for the same service.
    renew_resets: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # With ``renew_account_id``: «renew from scratch» (``RENEW_MODE_RESET``) sets
    # volume and time anew and drops the leftover, and never writes panel
    # groups. A third operation next to extend (NULL) and the plan switch
    # (``renew_resets``); set only when the buyer chose it on a summary that
    # says so, and dispatched on this value alone, never inferred.
    renew_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The store group this order was sold from (slug, never the row id), so a
    # later renew finds its group without guessing from panel groups.
    store_category_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A custom (گیگ/ماه) purchase: told from a legacy order that only lacks a
    # plan slug, which a renew may still match by shape.
    custom_purchase: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # The durable operation identity for trial recovery. NULL is legacy/unknown
    # history and must never be guessed as either kind.
    trial_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The store group a personal trial tested. One per person per group; NULL
    # is a trial from before groups had trials, and counts as the direct one.
    trial_category_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set by the admin «reset trial». A voided trial no longer blocks a new one
    # and stays in history — its panel account may still be live.
    trial_voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Delivery snapshot at order create (protects historical specs if Plan mutates).
    # NULL = legacy row; provisioning falls back to linked Plan (do not invent history).
    delivery_data_limit_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_hwid_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_group_ids: Mapped[list[int] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    # Renew intent committed BEFORE panel modify_user (anti double-extend).
    renew_target_expire_unix: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    renew_target_data_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The ASCII seed for the panel username, when the buyer gave one. A
    # reseller does not: their panel name is generated independently so their
    # own customer's Persian name cannot leak into it.
    service_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # «رضا | 09123456789» — the shop's own note, never an identifier. Separate
    # from `service_name` on purpose: see `services/customer_label.py`.
    customer_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    renew_account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("pasarguard_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    panel_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Idempotency of «🎁 ساخت سرویس برای مشتری»: 8 hex characters, unique.
    admin_request_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Catalog identity of the plan that was bought. Row ids change on every
    # publish, so renew looks this slug up in the live catalog.
    store_package_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    panel_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="orders")
    plan: Mapped[Plan] = relationship("Plan", back_populates="orders")
    payment: Mapped[Payment] = relationship("Payment", back_populates="order", uselist=False)
    referral_logs: Mapped[list[ReferralLog]] = relationship("ReferralLog", back_populates="order")
