"""A service the lifecycle job deleted is not a service any more, anywhere.

The row stays (orders, payments and the wallet ledger point at it), so every
loader has to skip it. Two layers: the shared loaders are run against a deleted
account, and a source scan fails the build when a new ``select(PasarGuardAccount…)``
appears without ``live_account_clause()`` and without being on the reviewed list.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401 - register mappers
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.broadcast import BroadcastAudience
from database.models.plan import DataLimitResetStrategy
from services.accounts import (
    find_account_id_for_order,
    get_account_overview,
    get_live_account,
    list_named_services,
    list_services_page,
)
from services.broadcast import _audience_query
from services.customer_lookup import (
    UserFilter,
    find_customers,
    get_customer_card,
    list_users,
)
from services.orders import find_renewable_account

REPO = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _shop(session: AsyncSession) -> tuple[User, PasarGuardAccount, PasarGuardAccount]:
    user = User(telegram_id=7, first_name="A", referral_code="SOFT0001", language_code="fa")
    session.add(user)
    await session.flush()
    live = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=1,
        panel_username="live_one",
        display_name="زنده",
        service_name="live_one",
        plan_fingerprint="volume:1:30:10",
    )
    gone = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=2,
        panel_username="gone_one",
        display_name="حذف‌شده",
        service_name="gone_one",
        plan_fingerprint="volume:1:30:10",
        deleted_at=datetime.now(UTC),
    )
    session.add_all([live, gone])
    await session.commit()
    return user, live, gone


@pytest.mark.asyncio
async def test_the_customers_list_and_overview_skip_a_deleted_service(
    session: AsyncSession,
) -> None:
    user, live, gone = await _shop(session)
    assert [a.id for a in await list_named_services(session, user.id)] == [live.id]
    from unittest.mock import AsyncMock

    overview = await get_account_overview(
        session, user_id=user.id, client=AsyncMock(), account_id=gone.id
    )
    assert overview.has_account is False
    assert await get_live_account(session, gone.id) is None
    assert (await get_live_account(session, live.id)) is not None


@pytest.mark.asyncio
async def test_the_resellers_paged_list_and_its_total_skip_a_deleted_service(
    session: AsyncSession,
) -> None:
    user, live, _gone = await _shop(session)
    page = await list_services_page(session, user.id)
    assert [a.id for a in page.rows] == [live.id]
    assert page.total == 1
    searched = await list_services_page(session, user.id, query="gone")
    assert searched.rows == [] and searched.total == 0


@pytest.mark.asyncio
async def test_a_reminder_does_not_find_the_account_of_a_deleted_service(
    session: AsyncSession,
) -> None:
    user, live, gone = await _shop(session)
    plan = Plan(
        name="p",
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=1,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add(plan)
    await session.flush()

    def order(**kw: object) -> Order:
        return Order(
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=1,
            discount_toman=0,
            **kw,
        )

    by_name, by_renew = order(panel_username="gone_one"), order(renew_account_id=gone.id)
    live_by_name = order(panel_username="live_one")
    session.add_all([by_name, by_renew, live_by_name])
    await session.commit()
    assert await find_account_id_for_order(session, by_name) is None
    assert await find_account_id_for_order(session, by_renew) is None
    assert await find_account_id_for_order(session, live_by_name) == live.id


@pytest.mark.asyncio
async def test_the_admin_card_search_and_filters_skip_a_deleted_service(
    session: AsyncSession,
) -> None:
    user, live, gone = await _shop(session)
    card = await get_customer_card(session, user.id)
    assert [s.account_id for s in card.services] == [live.id]
    assert [u.id for u in await find_customers(session, "gone_one")] == []
    assert [u.id for u in await find_customers(session, "live_one")] == [user.id]

    # A customer whose only service was deleted has no service.
    await session.refresh(live)
    live.deleted_at = datetime.now(UTC)
    await session.commit()
    with_service = await list_users(session, user_filter=UserFilter.WITH_SERVICE)
    without = await list_users(session, user_filter=UserFilter.NO_SERVICE)
    assert [r.user_id for r in with_service.rows] == []
    assert [r.user_id for r in without.rows] == [user.id]
    assert gone.id != live.id


@pytest.mark.asyncio
async def test_renew_matching_and_the_broadcast_audience_skip_a_deleted_service(
    session: AsyncSession,
) -> None:
    user, live, gone = await _shop(session)
    found = await find_renewable_account(session, user.id, "volume:1:30:10")
    assert found is not None and found.id == live.id
    live.deleted_at = datetime.now(UTC)
    await session.commit()
    assert await find_renewable_account(session, user.id, "volume:1:30:10") is None
    audience = (
        (await session.execute(_audience_query(BroadcastAudience.with_service))).scalars().all()
    )
    assert audience == []
    assert gone.deleted_at is not None
    assert datetime.now(UTC) - timedelta(minutes=1) < gone.deleted_at.replace(tzinfo=UTC)


# --- no loader forgets it ----------------------------------------------------

# Reviewed: these read an account on purpose without the filter.
_ON_PURPOSE = {
    # Ownership must include tombstones: never reassign a reserved identity.
    # Linking explicitly refuses deleted/pending-delete records; import aborts
    # on any existing mapping. Dedicated regression tests cover both cases.
    ("services/account_linking.py", "_mapping"),
    ("services/mirza_import.py", "apply_migration"),
    # Frozen-import verification must see tombstones and reject the binding,
    # including unexpected reserved identities. It never restores/deletes them.
    # test_mirza_reconcile covers both deleted and pending-delete refusal.
    ("services/mirza_reconcile.py", "reconcile_migration"),
    # An ACTIVE order's own account, found even when deleted: idempotent resume.
    ("services/provisioning.py", "_load_account_for_order"),
    # A re-created panel user revives its row (the unique index keeps one per name).
    ("services/provisioning.py", "_provision_create"),
    # A trial resume with a deleted account fails closed: the panel read is a 404
    # and the customer is told the trial is gone (`trial_panel_missing`).
    ("services/trial.py", "_resume_personal_trial"),
    ("services/trial.py", "resume_reseller_trial"),
    # The deletion list shows what it did: a deleted service stays in its own list, marked ✅.
    ("services/lifecycle_batches.py", "batch_rows"),
    # The «your service was deleted» notice is about an account that is deleted by now.
    ("services/lifecycle_run.py", "send_deleted_notices"),
}


def _selects_of_accounts(path: Path) -> list[tuple[str, int, bool]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int, bool]] = []

    def visit(node: ast.AST, function: str | None, source_of: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name, child)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "select"
                and any("PasarGuardAccount" in ast.unparse(a) for a in child.args)
            ):
                text = (
                    ast.get_source_segment(path.read_text(encoding="utf-8"), source_of)
                    if source_of
                    else ""
                )
                found.append(
                    (function or "<module>", child.lineno, "live_account_clause" in (text or ""))
                )
            visit(child, function, source_of)

    visit(tree, None, None)
    return found


def test_every_select_of_accounts_goes_through_the_live_clause_or_is_reviewed() -> None:
    offenders: list[str] = []
    for folder in ("services", "bot", "scheduler"):
        for path in sorted((REPO / folder).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            for function, line, has_clause in _selects_of_accounts(path):
                if has_clause or (rel, function) in _ON_PURPOSE:
                    continue
                offenders.append(f"{rel}:{line} in {function}()")
    assert offenders == [], (
        "select(PasarGuardAccount…) without live_account_clause() "
        f"(add it, or review and list it in _ON_PURPOSE): {offenders}"
    )
