"""STORE_CORE: versioned store catalog tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-12

- store_versions (draft|published|archived; pricing_document JSON)
- store_categories (stable slug per version; panel_group_ids; sales_flags)
- store_packages (ready|custom_template; price/pricing_ref; renew/new flags)
SQLite + Postgres safe. At-most-one published enforced in services.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    json_type = postgresql.JSONB() if dialect == "postgresql" else sa.JSON()

    store_version_status = sa.Enum(
        "draft",
        "published",
        "archived",
        name="store_version_status",
        create_constraint=True,
    )
    store_package_kind = sa.Enum(
        "ready",
        "custom_template",
        name="store_package_kind",
        create_constraint=True,
    )

    op.create_table(
        "store_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", store_version_status, nullable=False),
        sa.Column("pricing_document", json_type, nullable=False),
        sa.Column("purchase_steps", json_type, nullable=False),
        sa.Column("sales_rules", json_type, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="ix_store_versions_version"),
    )
    op.create_index("ix_store_versions_status", "store_versions", ["status"])

    op.create_table(
        "store_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("store_version_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title_fa", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("panel_group_ids", json_type, nullable=False),
        sa.Column("sales_flags", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_version_id"], ["store_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_version_id", "slug", name="uq_store_categories_version_slug"),
    )
    op.create_index(
        "ix_store_categories_version_sort",
        "store_categories",
        ["store_version_id", "sort_order"],
    )

    op.create_table(
        "store_packages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("store_version_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("kind", store_package_kind, nullable=False),
        sa.Column("title_fa", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("fixed_price_toman", sa.BigInteger(), nullable=True),
        sa.Column("pricing_ref", sa.String(length=64), nullable=True),
        sa.Column("options", json_type, nullable=False),
        sa.Column("forbid_extra_discount", sa.Boolean(), nullable=False),
        sa.Column("allow_new_sale", sa.Boolean(), nullable=False),
        sa.Column("allow_renew", sa.Boolean(), nullable=False),
        sa.Column("sales_flags", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["store_categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_version_id"], ["store_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_version_id", "slug", name="uq_store_packages_version_slug"),
    )
    op.create_index(
        "ix_store_packages_category_sort",
        "store_packages",
        ["category_id", "sort_order"],
    )
    op.create_index("ix_store_packages_version", "store_packages", ["store_version_id"])


def downgrade() -> None:
    op.drop_index("ix_store_packages_version", table_name="store_packages")
    op.drop_index("ix_store_packages_category_sort", table_name="store_packages")
    op.drop_table("store_packages")
    op.drop_index("ix_store_categories_version_sort", table_name="store_categories")
    op.drop_table("store_categories")
    op.drop_index("ix_store_versions_status", table_name="store_versions")
    op.drop_table("store_versions")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS store_package_kind")
        op.execute("DROP TYPE IF EXISTS store_version_status")
