"""Store catalog (versioned) — separate from historical Order/Plan snapshots.

STORE_CORE schema: published StoreVersion.pricing_document drives calculate_price.
Categories/packages hold catalog identity + sales flags; admin CRUD is STORE_ADMIN.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


def _json_column() -> Any:
    return JSON().with_variant(JSONB, "postgresql")


class StoreVersionStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class StorePackageKind(str, enum.Enum):
    ready = "ready"
    custom_template = "custom_template"


class StoreVersion(Base):
    """Versioned store snapshot. At most one row may be ``published`` (enforced in service)."""

    __tablename__ = "store_versions"
    __table_args__ = (
        Index("ix_store_versions_status", "status"),
        Index("ix_store_versions_version", "version", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[StoreVersionStatus] = mapped_column(
        Enum(StoreVersionStatus, name="store_version_status", create_constraint=True),
        nullable=False,
        default=StoreVersionStatus.draft,
    )
    # Full pricing-engine document (currency, volume_plan, unlimited_plan, promo_codes, …).
    pricing_document: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False)
    # Composable purchase blocks (v1 — not a free branching engine).
    purchase_steps: Mapped[list[Any]] = mapped_column(_json_column(), nullable=False, default=list)
    # Global sales rules hooks (discount_combine, renew confirm flags, locked invariants).
    sales_rules: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="seed_json")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    categories: Mapped[list[StoreCategory]] = relationship(
        "StoreCategory",
        back_populates="store_version",
        cascade="all, delete-orphan",
    )
    packages: Mapped[list[StorePackage]] = relationship(
        "StorePackage",
        back_populates="store_version",
        cascade="all, delete-orphan",
    )
    package_prices: Mapped[list[StorePackagePrice]] = relationship(
        "StorePackagePrice",
        back_populates="store_version",
        cascade="all, delete-orphan",
    )


class StoreCategory(Base):
    """A product group within a store version.

    Groups nest **one** level: a top-level group (``parent_id is None``) may hold
    child groups, e.g. «حجمی» → «ویژه» / «اقتصادی». Deeper nesting is refused in
    the service layer so a customer never taps more than three times to reach a
    price.
    """

    __tablename__ = "store_categories"
    __table_args__ = (
        UniqueConstraint("store_version_id", "slug", name="uq_store_categories_version_slug"),
        Index("ix_store_categories_version_sort", "store_version_id", "sort_order"),
        Index("ix_store_categories_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("store_versions.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("store_categories.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title_fa: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Panel groups for this group's plans. Highest-priority source at provision
    # time; falls back to pasarguard_settings, then the env default.
    panel_group_ids: Mapped[list[Any]] = mapped_column(_json_column(), nullable=False, default=list)
    # Hooks: allow_new_sale, allow_renew, etc.
    sales_flags: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
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

    store_version: Mapped[StoreVersion] = relationship("StoreVersion", back_populates="categories")
    packages: Mapped[list[StorePackage]] = relationship(
        "StorePackage",
        back_populates="category",
        cascade="all, delete-orphan",
    )
    children: Mapped[list[StoreCategory]] = relationship(
        "StoreCategory",
        back_populates="parent",
        cascade="all, delete-orphan",
        remote_side=None,
        foreign_keys=lambda: [StoreCategory.parent_id],
    )
    parent: Mapped[StoreCategory | None] = relationship(
        "StoreCategory",
        back_populates="children",
        remote_side=lambda: [StoreCategory.id],
        foreign_keys=lambda: [StoreCategory.parent_id],
    )


class StorePackage(Base):
    """Ready package (fixed price) or custom_template (options + pricing_ref)."""

    __tablename__ = "store_packages"
    __table_args__ = (
        UniqueConstraint("store_version_id", "slug", name="uq_store_packages_version_slug"),
        Index("ix_store_packages_category_sort", "category_id", "sort_order"),
        Index("ix_store_packages_version", "store_version_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("store_versions.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("store_categories.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[StorePackageKind] = mapped_column(
        Enum(StorePackageKind, name="store_package_kind", create_constraint=True),
        nullable=False,
    )
    title_fa: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Ready packages: independent fixed price. Custom: null + pricing_ref.
    fixed_price_toman: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pricing_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False, default=dict)
    forbid_extra_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_new_sale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sales_flags: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
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

    store_version: Mapped[StoreVersion] = relationship("StoreVersion", back_populates="packages")
    category: Mapped[StoreCategory] = relationship("StoreCategory", back_populates="packages")


class StorePackagePrice(Base):
    """Price cell for a ready plan: package_slug × duration × tier.

    Keyed on slug (not package id) because publishing clones mint new row ids.
    ``duration_days`` NULL means «بدون انقضا».
    """

    __tablename__ = "store_package_prices"
    __table_args__ = (
        UniqueConstraint(
            "store_version_id",
            "package_slug",
            "duration_days",
            "tier",
            name="uq_store_package_prices_cell",
        ),
        Index("ix_store_package_prices_version_slug", "store_version_id", "package_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("store_versions.id", ondelete="CASCADE"), nullable=False
    )
    package_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    price_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    store_version: Mapped[StoreVersion] = relationship(
        "StoreVersion", back_populates="package_prices"
    )
