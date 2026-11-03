"""T-007 — reseller price matrix (duration × tier) + percent preview/undo."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import database.models  # noqa: F401
from database.base import Base
from database.models import ResellerPlanPrice, ResellerPriceOp, User
from services.reseller import (
    apply_percent_cut,
    get_plan_price,
    list_plan_prices,
    percent_cut_preview,
    quote_ready_plan,
    round_percent_cut,
    set_plan_price,
    undo_percent_cut,
)
from services.store_pricing import DEFAULT_TIER


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _shop(session: AsyncSession) -> User:
    user = User(telegram_id=900001, referral_code="SHOP900001", is_reseller=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_price_for_60_not_returned_for_30(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=60, tier=DEFAULT_TIER)
    assert (
        await get_plan_price(session, shop.id, "vol-a", duration_days=30, tier=DEFAULT_TIER) is None
    )
    assert (
        await get_plan_price(session, shop.id, "vol-a", duration_days=60, tier=DEFAULT_TIER)
        == 200_000
    )


@pytest.mark.asyncio
async def test_none_duration_distinct_from_zero(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 100_000, duration_days=None, tier=DEFAULT_TIER)
    await set_plan_price(session, shop.id, "vol-a", 90_000, duration_days=0, tier=DEFAULT_TIER)
    assert (
        await get_plan_price(session, shop.id, "vol-a", duration_days=None, tier=DEFAULT_TIER)
        == 100_000
    )
    assert (
        await get_plan_price(session, shop.id, "vol-a", duration_days=0, tier=DEFAULT_TIER)
        == 90_000
    )


@pytest.mark.asyncio
async def test_percent_op_only_touches_visible_tab(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=30, tier=DEFAULT_TIER)
    await set_plan_price(session, shop.id, "vol-a", 300_000, duration_days=60, tier=DEFAULT_TIER)
    other_before = await list_plan_prices(session, shop.id, duration_days=60, tier=DEFAULT_TIER)
    agreed = await list_plan_prices(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    changes = percent_cut_preview(agreed, pct=10)
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=30,
        tier=DEFAULT_TIER,
        pct=10,
        changes=changes,
    )
    await session.commit()
    other_after = await list_plan_prices(session, shop.id, duration_days=60, tier=DEFAULT_TIER)
    assert other_after == other_before
    assert other_after["vol-a"] == 300_000


@pytest.mark.asyncio
async def test_percent_skips_cells_without_agreed_price(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "priced", 200_000, duration_days=30, tier=DEFAULT_TIER)
    agreed = await list_plan_prices(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    # retail would exist for "unpriced" but it is absent from agreed
    changes = percent_cut_preview(agreed, pct=10)
    assert all(slug != "unpriced" for slug, *_ in changes)
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=30,
        tier=DEFAULT_TIER,
        pct=10,
        changes=changes,
    )
    await session.commit()
    assert (
        await get_plan_price(session, shop.id, "unpriced", duration_days=30, tier=DEFAULT_TIER)
        is None
    )


def test_rounding_265000_at_10_percent() -> None:
    assert round_percent_cut(265_000, 10) == 238_000
    assert round_percent_cut(265_000, 10) != 238_500
    assert round_percent_cut(265_000, 10) != 239_000


@pytest.mark.asyncio
async def test_preview_writes_nothing(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    before_rows = (
        await session.execute(select(ResellerPlanPrice.price_toman, ResellerPlanPrice.package_slug))
    ).all()
    agreed = await list_plan_prices(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    _ = percent_cut_preview(agreed, pct=10)
    after_rows = (
        await session.execute(select(ResellerPlanPrice.price_toman, ResellerPlanPrice.package_slug))
    ).all()
    assert after_rows == before_rows
    ops = (await session.execute(select(ResellerPriceOp))).scalars().all()
    assert ops == []


@pytest.mark.asyncio
async def test_undo_restores_including_null_before(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "keep", 200_000, duration_days=30, tier=DEFAULT_TIER)
    await set_plan_price(session, shop.id, "seeded", 180_000, duration_days=30, tier=DEFAULT_TIER)
    # Op claims seeded had no agreed price before (null) — undo must delete it.
    session.add(
        ResellerPriceOp(
            user_id=shop.id,
            duration_days=30,
            tier=DEFAULT_TIER,
            before_json={"keep": 200_000, "seeded": None},
            created_at=datetime.now(UTC),
        )
    )
    await set_plan_price(session, shop.id, "keep", 180_000, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    restored = await undo_percent_cut(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    assert restored == 2
    assert (
        await get_plan_price(session, shop.id, "keep", duration_days=30, tier=DEFAULT_TIER)
        == 200_000
    )
    assert (
        await get_plan_price(session, shop.id, "seeded", duration_days=30, tier=DEFAULT_TIER)
        is None
    )


@pytest.mark.asyncio
async def test_undo_twice_second_is_noop(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=30, tier=DEFAULT_TIER)
    changes = percent_cut_preview({"vol-a": 200_000}, pct=10)
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=30,
        tier=DEFAULT_TIER,
        pct=10,
        changes=changes,
    )
    await session.commit()
    first = await undo_percent_cut(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    second = await undo_percent_cut(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    assert first == 1
    assert second is None


@pytest.mark.asyncio
async def test_second_percent_replaces_op(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=30, tier=DEFAULT_TIER)
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=30,
        tier=DEFAULT_TIER,
        pct=10,
        changes=percent_cut_preview({"vol-a": 200_000}, pct=10),
    )
    mid = await get_plan_price(session, shop.id, "vol-a", duration_days=30, tier=DEFAULT_TIER)
    assert mid is not None
    await apply_percent_cut(
        session,
        shop.id,
        duration_days=30,
        tier=DEFAULT_TIER,
        pct=10,
        changes=percent_cut_preview({"vol-a": mid}, pct=10),
    )
    await session.commit()
    ops = (
        (await session.execute(select(ResellerPriceOp).where(ResellerPriceOp.user_id == shop.id)))
        .scalars()
        .all()
    )
    assert len(ops) == 1
    assert ops[0].before_json == {"vol-a": mid}
    await undo_percent_cut(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    assert (
        await get_plan_price(session, shop.id, "vol-a", duration_days=30, tier=DEFAULT_TIER) == mid
    )


def test_migration_backfill_reads_plan_duration(tmp_path: Path) -> None:
    """A row whose slug matches a 60-day published plan gets duration_days=60."""
    import importlib.util

    mig_path = Path("alembic/versions/h5i6j7k8l9m0_reseller_price_matrix.py")
    spec = importlib.util.spec_from_file_location("reseller_price_matrix_mig", mig_path)
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    _duration_from_options = mig._duration_from_options
    backfill_reseller_plan_durations = mig.backfill_reseller_plan_durations

    assert _duration_from_options({"duration_days": 60}) == 60
    assert _duration_from_options({"duration_months": 2}) == 60
    assert _duration_from_options({"duration_days": 0}) is None

    db = tmp_path / "mig.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE store_versions (" "id INTEGER PRIMARY KEY, status VARCHAR)")
        )
        conn.execute(
            text(
                "CREATE TABLE store_packages ("
                "id INTEGER PRIMARY KEY, store_version_id INTEGER, "
                "slug VARCHAR, kind VARCHAR, options JSON)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE reseller_plan_prices ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL, package_slug VARCHAR(128) NOT NULL, "
                "price_toman INTEGER NOT NULL, updated_at DATETIME NOT NULL, "
                "duration_days INTEGER, "
                "tier VARCHAR(16) NOT NULL DEFAULT 'economy')"
            )
        )
        conn.execute(text("INSERT INTO store_versions (id, status) VALUES (1, 'published')"))
        conn.execute(
            text(
                "INSERT INTO store_packages (id, store_version_id, slug, kind, options) "
                "VALUES (1, 1, 'sixty', 'ready', :opts)"
            ),
            {"opts": json.dumps({"duration_days": 60, "volume_gb": 40})},
        )
        now = datetime.now(UTC).isoformat()
        conn.execute(
            text(
                "INSERT INTO reseller_plan_prices "
                "(user_id, package_slug, price_toman, updated_at) VALUES "
                "(1, 'sixty', 200000, :now), (1, 'missing', 100000, :now)"
            ),
            {"now": now},
        )

    with Session(engine) as sess:
        total, matched, null_fallback = backfill_reseller_plan_durations(sess)
        sess.commit()
        assert total == 2
        assert matched == 1
        assert null_fallback == 1
        rows = {
            str(r[0]): r[1]
            for r in sess.execute(
                text("SELECT package_slug, duration_days FROM reseller_plan_prices")
            )
        }
    assert rows["sixty"] == 60
    assert rows["missing"] is None


@pytest.mark.asyncio
async def test_crossover_guard_same_cell(session: AsyncSession) -> None:
    shop = await _shop(session)
    await set_plan_price(session, shop.id, "vol-a", 200_000, duration_days=30, tier=DEFAULT_TIER)
    # Agreed above retail for this cell → charge retail
    quote = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="vol-a",
        retail_price_toman=150_000,
        duration_days=30,
        tier=DEFAULT_TIER,
    )
    assert quote.crossed_over is True
    assert quote.price_toman == 150_000
    # Different cell must not use the 30-day agreed price
    quote60 = await quote_ready_plan(
        session,
        user_id=shop.id,
        package_slug="vol-a",
        retail_price_toman=150_000,
        duration_days=60,
        tier=DEFAULT_TIER,
    )
    assert quote60.crossed_over is False
    assert quote60.price_toman == 150_000


# --- the preview must not write, proven at the handler ---------------------
#
# `percent_cut_preview` is a pure function, so a test that calls it directly can
# never catch the thing that matters: whether the *handler* writes before the
# owner has confirmed. The owner named this the sensitive part — «یه صفر اضافه»
# has to be visible before it is committed, not merely reversible afterwards —
# so the guard is tested where it actually lives.


@pytest.mark.asyncio
async def test_the_preview_handler_writes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.handlers.admin import admin_reseller_number_save
    from bot.states import AdminStates
    from services.store_admin import (
        commit_and_activate,
        create_draft_category,
        create_draft_package,
    )

    # The cut reaches only the plans on the screen, so the screen needs one.
    cat = await create_draft_category(session, title_fa="حجمی", kind="volume")
    plan = await create_draft_package(
        session,
        category_id=cat.id,
        title_fa="40 گیگ / 30 روز",
        fixed_price_toman=300_000,
        volume_gb=40,
        duration_days=30,
    )
    await commit_and_activate(session, notes="seed")
    shop = await _shop(session)
    await set_plan_price(session, shop.id, plan.slug, 265_000, duration_days=30, tier=DEFAULT_TIER)
    await session.commit()
    before = await list_plan_prices(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    assert before == {plan.slug: 265_000}

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(AdminStates.waiting_reseller_number)
    await state.update_data(
        rs_user_id=shop.id,
        rs_field="pct",
        rs_category_id=cat.id,
        rs_sel_duration_days=30,
        rs_sel_tier=DEFAULT_TIER,
    )
    message = SimpleNamespace(
        text="10", from_user=SimpleNamespace(id=4242), answer=AsyncMock(), edit_text=AsyncMock()
    )
    settings = SimpleNamespace(admin_telegram_ids=[4242])
    admin = SimpleNamespace(id=shop.id + 1, telegram_id=4242, is_admin=True)

    await admin_reseller_number_save(
        message,  # type: ignore[arg-type]
        state,
        session,
        settings,  # type: ignore[arg-type]
        admin,  # type: ignore[arg-type]
    )

    after = await list_plan_prices(session, shop.id, duration_days=30, tier=DEFAULT_TIER)
    assert after == before, "the preview committed a price change before any confirmation"
    ops = (await session.execute(select(ResellerPriceOp))).scalars().all()
    assert ops == [], "the preview wrote an undo snapshot, so something was applied"
    said = " ".join(str(c.args[0]) for c in message.answer.await_args_list if c.args)
    assert "هنوز چیزی ثبت نشده" in said, "the preview must say out loud that nothing is saved yet"
