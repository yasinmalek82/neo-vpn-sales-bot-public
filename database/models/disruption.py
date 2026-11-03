"""A customer's «this does not work here» report: ISP, city, which services.

Direct inbounds work on some ISPs and not others, and the owner fixes the ISPs
with the most reports first. Kept as rows rather than only report messages so
the nightly digest can count them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class DisruptionReport(Base):
    __tablename__ = "disruption_reports"
    __table_args__ = (Index("ix_disruption_reports_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # A key from services.disruption.ISPS, never free text: it is what gets counted.
    isp: Mapped[str] = mapped_column(String(32), nullable=False)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    # Store group titles of the reporter's live services, e.g. «نامحدود اقتصادی».
    service_groups: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
