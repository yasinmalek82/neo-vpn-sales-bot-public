from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.order import Order
    from database.models.pasarguard_account import PasarGuardAccount
    from database.models.payment import Payment
    from database.models.referral_log import ReferralLog
    from database.models.wallet import WalletLedgerEntry


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_telegram_id", "telegram_id", unique=True),
        Index("ix_users_referral_code", "referral_code", unique=True),
        Index("ix_users_referred_by_id", "referred_by_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str] = mapped_column(String(8), default="fa", nullable=False)
    referral_code: Mapped[str] = mapped_column(String(16), nullable=False)
    referred_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Kept separate from `is_admin` on purpose. Admin-ness is synced from
    # ADMIN_TELEGRAM_IDS on every request, so it is owned by the env; being a
    # reseller is granted by hand and owned by this table. Folding both into one
    # «role» column would give a single field two masters.
    is_reseller: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    legacy_trial_consumed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    trial_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # «آخرین زمان استفاده از ربات» on the admin's user card. Written by
    # `UserContextMiddleware` on every interaction, so it answers «is this
    # customer still around?» without asking them.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wallet_balance_toman: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
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
    referrer: Mapped[User | None] = relationship(
        "User", remote_side=[id], back_populates="referrals"
    )
    referrals: Mapped[list[User]] = relationship("User", back_populates="referrer")
    orders: Mapped[list[Order]] = relationship(
        "Order", back_populates="user", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="Payment.user_id",
    )
    referral_logs_referrer: Mapped[list[ReferralLog]] = relationship(
        "ReferralLog", foreign_keys="ReferralLog.referrer_id", back_populates="referrer"
    )
    referral_logs_referred: Mapped[list[ReferralLog]] = relationship(
        "ReferralLog", foreign_keys="ReferralLog.referred_id", back_populates="referred"
    )

    pasarguard_accounts: Mapped[list[PasarGuardAccount]] = relationship(
        "PasarGuardAccount",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="PasarGuardAccount.id.desc()",
    )
    wallet_ledger_entries: Mapped[list[WalletLedgerEntry]] = relationship(
        "WalletLedgerEntry",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="WalletLedgerEntry.id.desc()",
    )
