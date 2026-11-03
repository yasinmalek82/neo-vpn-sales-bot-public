"""Durable identity for manual extend / add-data operator commands.

An absolute panel write is not enough: a retry re-reads the already-changed
panel and recomputes a new absolute. Committing the intended target before the
panel call (and re-using that row on retry) is what makes one operator tap
idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.pasarguard_account import PasarGuardAccount

KIND_EXTEND_DAYS = "extend_days"
KIND_ADD_DATA = "add_data"
STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_SUPERSEDED = "superseded"


class ServiceOperation(Base):
    """One operator intent: pending until the panel accepts it, then applied."""

    __tablename__ = "service_operations"
    __table_args__ = (
        # At most one in-flight intent per account+kind. A second concurrent
        # create must fail at the database, not as a handler convention.
        Index(
            "uq_service_operations_pending_account_kind",
            "account_id",
            "kind",
            unique=True,
            sqlite_where=text("state = 'pending'"),
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_service_operations_account_id", "account_id", unique=False),
        Index("uq_service_operations_operation_key", "operation_key", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("pasarguard_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_expire_unix: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_data_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Days (extend_days) or GB (add_data) the operator asked for; NULL on rows
    # written before the column existed.
    requested_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[PasarGuardAccount] = relationship("PasarGuardAccount")
