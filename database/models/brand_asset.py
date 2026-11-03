"""Owner-uploaded brand images, keyed by what they are for.

One row per key (today only ``qr_background``). Stored in the database rather
than on disk so it survives a container rebuild and a restore from backup.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class BrandAsset(Base):
    __tablename__ = "brand_assets"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
