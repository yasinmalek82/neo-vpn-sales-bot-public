"""Permanent amount reservations and privacy-minimal bank notification audit."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class BankInvoice(Base):
    __tablename__ = "bank_invoices"
    __table_args__ = (
        UniqueConstraint("account_key", "amount_toman", name="uq_bank_invoice_account_amount"),
        CheckConstraint("suffix_toman BETWEEN 1 AND 999", name="ck_bank_invoice_suffix"),
        CheckConstraint(
            "base_due_toman > 0 AND amount_toman = base_due_toman + suffix_toman",
            name="ck_bank_invoice_amount",
        ),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True
    )
    account_key: Mapped[str] = mapped_column(String(64), nullable=False)
    card_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_due_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    suffix_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BankSmsEvent(Base):
    __tablename__ = "bank_sms_events"

    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
