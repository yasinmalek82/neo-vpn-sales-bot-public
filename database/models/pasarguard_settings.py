"""Singleton row for in-bot PasarGuard panel settings (Phase A2)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

# Fixed primary key for the singleton settings row.
PASARGUARD_SETTINGS_ROW_ID = 1


class PasarGuardSettings(Base):
    """DB override for PasarGuard connection; env remains fallback when empty."""

    __tablename__ = "pasarguard_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    admin_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    admin_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_path: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_group_ids: Mapped[list | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    # Extensible map: {"volume":[7],"unlimited":[1]} — keys are PlanType strings.
    product_group_ids: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    timeout: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Trial shape lives here, not in .env: the trial account is created on the
    # panel, so its size belongs beside the panel settings that provision it —
    # and the owner can change it from Telegram instead of editing a file and
    # restarting. NULL means "fall back to the env default".
    trial_data_limit_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trial_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trial_hwid_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        nullable=True,
    )

    def __repr__(self) -> str:
        # admin_username / admin_password columns retained but unused (API key only).
        key = "••••" if self.api_key else None
        return (
            f"PasarGuardSettings(id={self.id!r}, base_url={self.base_url!r}, "
            f"api_key={key!r}, "
            f"sub_path={self.sub_path!r}, timeout={self.timeout!r})"
        )
