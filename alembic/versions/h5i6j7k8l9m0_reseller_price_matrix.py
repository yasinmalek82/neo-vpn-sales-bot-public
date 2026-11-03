"""reseller_plan_prices gains duration_days × tier; reseller_price_ops undo table

Revision ID: h5i6j7k8l9m0
Revises: g4h5i6j7k8l9
Create Date: 2026-09-20

Backfill duration_days from the published ready plan's options — never a
constant — so an agreed price stays pointed at the duration it was agreed for.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import op

revision: str = "h5i6j7k8l9m0"
down_revision: str | None = "g4h5i6j7k8l9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LAST_BACKFILL_COUNTS: tuple[int, int, int] | None = None


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
            days = int(options["duration_days"])
            return None if days == 0 else days
        if options.get("duration_months") is not None:
            months = int(options["duration_months"])
            return None if months == 0 else months * 30
    except (TypeError, ValueError):
        return None
    return None


def backfill_reseller_plan_durations(session: Session) -> tuple[int, int, int]:
    """Fill duration_days from published ready plans. Returns (total, matched, null_fallback)."""
    versions = sa.table(
        "store_versions",
        sa.column("id", sa.Integer),
        sa.column("status", sa.String),
    )
    packages = sa.table(
        "store_packages",
        sa.column("id", sa.Integer),
        sa.column("store_version_id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("kind", sa.String),
        sa.column("options", sa.JSON),
    )
    rpp = sa.table(
        "reseller_plan_prices",
        sa.column("id", sa.Integer),
        sa.column("package_slug", sa.String),
        sa.column("duration_days", sa.Integer),
        sa.column("tier", sa.String),
    )

    # The column is a Postgres enum. Binding the literal as varchar has no
    # operator against that type; SQLite stores the same value as text.
    if session.get_bind().dialect.name == "postgresql":
        published_filter = sa.text("status::text = 'published'")
    else:
        published_filter = versions.c.status == "published"
    published_ids = [
        int(r[0]) for r in session.execute(select(versions.c.id).where(published_filter)).all()
    ]
    slug_duration: dict[str, int | None] = {}
    if published_ids:
        pkg_rows = (
            session.execute(
                select(packages.c.slug, packages.c.kind, packages.c.options).where(
                    packages.c.store_version_id.in_(published_ids)
                )
            )
            .mappings()
            .all()
        )
        for prow in pkg_rows:
            kind_val = str(getattr(prow["kind"], "value", prow["kind"]))
            if kind_val != "ready":
                continue
            slug = str(prow["slug"])
            if slug not in slug_duration:
                slug_duration[slug] = _duration_from_options(prow["options"])

    rows = session.execute(select(rpp.c.id, rpp.c.package_slug)).mappings().all()
    matched = 0
    null_fallback = 0
    for row in rows:
        slug = str(row["package_slug"])
        if slug in slug_duration:
            duration = slug_duration[slug]
            matched += 1
        else:
            duration = None
            null_fallback += 1
        session.execute(
            rpp.update()
            .where(rpp.c.id == int(row["id"]))
            .values(duration_days=duration, tier="economy")
        )
    session.flush()
    return len(rows), matched, null_fallback


def upgrade() -> None:
    op.create_table(
        "reseller_price_ops",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reseller_price_ops_user_tab",
        "reseller_price_ops",
        ["user_id", "duration_days", "tier"],
        unique=False,
    )

    with op.batch_alter_table("reseller_plan_prices") as batch:
        batch.add_column(sa.Column("duration_days", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "tier",
                sa.String(length=16),
                nullable=False,
                server_default="economy",
            )
        )

    bind = op.get_bind()
    session = Session(bind=bind)
    total, matched, null_fallback = backfill_reseller_plan_durations(session)
    session.commit()
    # Surfaced in handoff report (alembic stdout is often swallowed).
    global LAST_BACKFILL_COUNTS  # noqa: PLW0603
    LAST_BACKFILL_COUNTS = (total, matched, null_fallback)

    with op.batch_alter_table("reseller_plan_prices") as batch:
        batch.drop_constraint("uq_reseller_plan_price", type_="unique")
        batch.create_unique_constraint(
            "uq_reseller_plan_price_cell",
            ["user_id", "package_slug", "duration_days", "tier"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    rpp = sa.table(
        "reseller_plan_prices",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.BigInteger),
        sa.column("package_slug", sa.String),
        sa.column("duration_days", sa.Integer),
        sa.column("tier", sa.String),
    )
    session.execute(rpp.delete().where(rpp.c.tier != "economy"))
    kept: set[tuple[int, str]] = set()
    rows = (
        session.execute(select(rpp.c.id, rpp.c.user_id, rpp.c.package_slug).order_by(rpp.c.id))
        .mappings()
        .all()
    )
    drop_ids: list[int] = []
    for row in rows:
        key = (int(row["user_id"]), str(row["package_slug"]))
        if key in kept:
            drop_ids.append(int(row["id"]))
        else:
            kept.add(key)
    if drop_ids:
        session.execute(rpp.delete().where(rpp.c.id.in_(drop_ids)))
    session.commit()

    with op.batch_alter_table("reseller_plan_prices") as batch:
        batch.drop_constraint("uq_reseller_plan_price_cell", type_="unique")
        batch.drop_column("duration_days")
        batch.drop_column("tier")
        batch.create_unique_constraint(
            "uq_reseller_plan_price",
            ["user_id", "package_slug"],
        )

    op.drop_index("ix_reseller_price_ops_user_tab", table_name="reseller_price_ops")
    op.drop_table("reseller_price_ops")
