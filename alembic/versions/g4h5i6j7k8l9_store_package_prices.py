"""store_package_prices matrix + category duration defaults

Revision ID: g4h5i6j7k8l9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-19

A ready plan becomes a volume; its price lives in ``store_package_prices``
keyed on (version, slug, duration_days, tier). ``fixed_price_toman`` is left
in place and used only as the backfill source.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import op

revision: str = "g4h5i6j7k8l9"
down_revision: str | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _duration_from_options(options: object) -> int | None:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return None
    if not isinstance(options, dict):
        return None
    try:
        if options.get("duration_days") is not None:
            return int(options["duration_days"])
        if options.get("duration_months") is not None:
            return int(options["duration_months"]) * 30
    except (TypeError, ValueError):
        return None
    return None


def _as_flags(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def upgrade() -> None:
    op.create_table(
        "store_package_prices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("store_version_id", sa.Integer(), nullable=False),
        sa.Column("package_slug", sa.String(length=64), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("price_toman", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_version_id"], ["store_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "store_version_id",
            "package_slug",
            "duration_days",
            "tier",
            name="uq_store_package_prices_cell",
        ),
    )
    op.create_index(
        "ix_store_package_prices_version_slug",
        "store_package_prices",
        ["store_version_id", "package_slug"],
        unique=False,
    )

    bind = op.get_bind()
    session = Session(bind=bind)
    now = datetime.now(UTC)

    packages = sa.table(
        "store_packages",
        sa.column("id", sa.Integer),
        sa.column("store_version_id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("kind", sa.String),
        sa.column("fixed_price_toman", sa.BigInteger),
        sa.column("options", sa.JSON),
        sa.column("category_id", sa.Integer),
    )
    prices = sa.table(
        "store_package_prices",
        sa.column("store_version_id", sa.Integer),
        sa.column("package_slug", sa.String),
        sa.column("duration_days", sa.Integer),
        sa.column("tier", sa.String),
        sa.column("price_toman", sa.BigInteger),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    categories = sa.table(
        "store_categories",
        sa.column("id", sa.Integer),
        sa.column("sales_flags", sa.JSON),
    )

    # kind may be an enum name or value depending on dialect; match both.
    rows = session.execute(select(packages)).mappings().all()
    durations_by_cat: dict[int, set[int | None]] = {}
    price_rows: list[dict] = []
    for row in rows:
        kind = row["kind"]
        kind_val = str(getattr(kind, "value", kind))
        if kind_val != "ready":
            continue
        if row["fixed_price_toman"] is None:
            continue
        duration = _duration_from_options(row["options"])
        price_rows.append(
            {
                "store_version_id": int(row["store_version_id"]),
                "package_slug": str(row["slug"]),
                "duration_days": duration,
                "tier": "economy",
                "price_toman": int(row["fixed_price_toman"]),
                "created_at": now,
                "updated_at": now,
            }
        )
        cat_id = int(row["category_id"])
        durations_by_cat.setdefault(cat_id, set()).add(duration)

    if price_rows:
        session.execute(prices.insert(), price_rows)

    cat_rows = session.execute(select(categories)).mappings().all()
    for crow in cat_rows:
        flags = _as_flags(crow["sales_flags"])
        cat_id = int(crow["id"])
        durs = durations_by_cat.get(cat_id)
        if durs:
            # stable order: ints ascending, None last
            ordered = sorted(d for d in durs if d is not None)
            if None in durs:
                ordered_list: list[int | None] = list(ordered) + [None]
            else:
                ordered_list = list(ordered)
            flags["durations"] = ordered_list
        else:
            flags.setdefault("durations", [30])
        flags.setdefault("tiers_enabled", False)
        flags.setdefault("premium_panel_group_ids", [])
        session.execute(
            categories.update().where(categories.c.id == cat_id).values(sales_flags=flags)
        )

    session.commit()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    categories = sa.table(
        "store_categories",
        sa.column("id", sa.Integer),
        sa.column("sales_flags", sa.JSON),
    )
    cat_rows = session.execute(select(categories)).mappings().all()
    for crow in cat_rows:
        flags = _as_flags(crow["sales_flags"])
        for key in ("durations", "tiers_enabled", "premium_panel_group_ids"):
            flags.pop(key, None)
        session.execute(
            categories.update().where(categories.c.id == int(crow["id"])).values(sales_flags=flags)
        )
    session.commit()

    op.drop_index("ix_store_package_prices_version_slug", table_name="store_package_prices")
    op.drop_table("store_package_prices")
