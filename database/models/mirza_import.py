"""Immutable source namespace and per-record migration mapping/history."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class MirzaBatch(Base):
    __tablename__ = "mirza_import_batches"
    __table_args__ = (
        CheckConstraint(
            "source_unit IS NULL OR source_unit IN ('toman', 'rial')", name="ck_mirza_source_unit"
        ),
    )

    namespace: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class MirzaRecord(Base):
    __tablename__ = "mirza_import_records"
    __table_args__ = (Index("ix_mirza_import_records_user", "target_user_id"),)

    namespace: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("mirza_import_batches.namespace", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_table: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    target_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pasarguard_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    opening_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("wallet_ledger_entries.id", ondelete="RESTRICT"), nullable=True
    )
