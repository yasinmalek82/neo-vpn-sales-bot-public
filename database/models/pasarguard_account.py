"""Mapping between internal users and PasarGuard panel accounts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.user import User


class PasarGuardAccount(Base):
    __tablename__ = "pasarguard_accounts"
    __table_args__ = (
        Index("ix_pasarguard_accounts_user_id", "user_id", unique=False),
        Index("ix_pasarguard_accounts_panel_username", "panel_username", unique=True),
        Index("ix_pasarguard_accounts_panel_user_id", "panel_user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    panel_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    panel_username: Mapped[str] = mapped_column(String(64), nullable=False)
    subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The first moment a lifecycle run saw the panel user «limited», cleared the
    # first time a run sees anything else. The panel does not say WHEN a user ran
    # out of volume, so the «N days after the volume ran out» clock starts here.
    limited_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft delete: the panel user was deleted by the lifecycle job. The row, its
    # orders, payments and ledger entries stay (finance and history do not
    # change); every account loader skips it (`services/accounts.py`). The unique
    # index on `panel_username` still holds the name.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Two-phase delete: the intent is committed BEFORE the panel call, so a crash
    # between the panel's delete and our commit converges (the next run sees the
    # intent and a 404 for this user, and finishes the soft delete).
    delete_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The owner's «حذف نشود»: the lifecycle job and an approved list both keep it.
    delete_exempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
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

    user: Mapped[User] = relationship("User", back_populates="pasarguard_accounts")
