"""Persistent-session recovery for the one personal trial operation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.report import TOPIC_SALES, ReportOutbox
from services.trial import TrialError, TrialOffer, TrialResult, issue_free_trial


def test_trial_kind_migration_backfills_only_proven_personal_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration must leave ambiguous and unclaimed legacy trials unknown."""
    database_path = tmp_path / "trial-kind.db"
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "i6j7k8l9m0n1")

    connection = sqlite3.connect(database_path)
    now = "2026-09-21 00:00:00"
    connection.executemany(
        """
        INSERT INTO users (
            id, telegram_id, language_code, referral_code, is_admin, is_reseller,
            is_banned, wallet_balance_toman, trial_claimed_at, created_at, updated_at
        ) VALUES (?, ?, 'fa', ?, 0, 0, 0, 0, ?, ?, ?)
        """,
        [
            (1, 901, "ONE", now, now, now),
            (2, 902, "MANY", now, now, now),
            (3, 903, "NONE", None, now, now),
        ],
    )
    connection.execute(
        """
        INSERT INTO plans (
            id, name, data_limit_gb, duration_days, price_toman,
            data_limit_reset_strategy, group_ids, hwid_limit, is_active,
            sort_order, created_at, updated_at
        ) VALUES (1, 'trial-5GB-1d', 5, 1, 0, 'no_reset', '[]', 1, 1, 0, ?, ?)
        """,
        (now, now),
    )
    connection.executemany(
        """
        INSERT INTO orders (
            id, user_id, plan_id, status, price_toman, discount_toman,
            wallet_applied_toman, force_new, created_at, updated_at
        ) VALUES (?, ?, 1, 'pending', 0, 0, 0, 0, ?, ?)
        """,
        [(1, 1, now, now), (2, 2, now, now), (3, 2, now, now), (4, 3, now, now)],
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "j7k8l9m0n1o2")  # pinned: the revision under test
    connection = sqlite3.connect(database_path)
    rows = connection.execute("SELECT id, trial_kind FROM orders ORDER BY id").fetchall()
    connection.close()
    assert rows == [(1, "personal"), (2, None), (3, None), (4, None)]


def _panel_user(username: str, *, panel_id: int = 500) -> UserResponse:
    return UserResponse(
        id=panel_id,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=5 * 1024**3,
        expire=1_800_000_000,
    )


async def _seed_user(factory: async_sessionmaker[AsyncSession], telegram_id: int = 701) -> int:
    async with factory() as session:
        user = User(telegram_id=telegram_id, referral_code=f"TRIAL{telegram_id}")
        session.add(user)
        await session.commit()
        return int(user.id)


def _client(*, panel_users: dict[str, UserResponse], create: Any) -> AsyncMock:
    async def get_user(username: str) -> UserResponse:
        if username in panel_users:
            return panel_users[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=create)
    client.subscription_url_from_response = lambda user: user.subscription_url
    return client


@pytest.mark.asyncio
async def test_lost_personal_trial_reply_reopens_same_durable_operation(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Removing recovery must create a second order/account after a restart."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory)
    panel_users: dict[str, UserResponse] = {}
    created: list[str] = []

    async def accept_then_lose_reply(payload: Any) -> UserResponse:
        username = str(payload.username)
        created.append(username)
        panel_users[username] = _panel_user(username)
        raise TimeoutError("reply lost after panel accepted")

    client = _client(panel_users=panel_users, create=accept_then_lose_reply)
    async with factory() as session_a:
        with pytest.raises(TimeoutError, match="reply lost"):
            await issue_free_trial(
                session_a,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
        await session_a.rollback()

    async with factory() as session_b:
        result = await issue_free_trial(
            session_b,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=99,
            duration_days=99,
            hwid_limit=9,
            group_ids=[99],
        )
        await session_b.commit()
        assert result.order.trial_kind == "personal"
        assert result.order.delivery_data_limit_gb == 5
        assert result.order.delivery_duration_days == 1
        assert result.order.delivery_hwid_limit == 1
        assert result.order.delivery_group_ids == [7]
        assert result.account.plan_fingerprint == "trial:5:1"
        assert result.account.panel_username == created[0]
        assert result.already_claimed is False
        assert (
            await session_b.scalar(
                select(func.count()).select_from(Order).where(Order.user_id == user_id)
            )
            == 1
        )
        reports = list((await session_b.execute(select(ReportOutbox))).scalars())
        assert len(reports) == 1
        assert reports[0].topic_key != TOPIC_SALES
    assert len(created) == 1


@pytest.mark.asyncio
async def test_terminal_personal_trial_rejection_retries_the_same_order(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A 422 must not consume another order after the panel configuration is fixed."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=702)
    panel_users: dict[str, UserResponse] = {}

    async def reject(payload: Any) -> UserResponse:
        raise PasarGuardAPIError("bad groups", status_code=422)

    client = _client(panel_users=panel_users, create=reject)
    async with factory() as session_a:
        with pytest.raises(PasarGuardAPIError, match="bad groups"):
            await issue_free_trial(
                session_a,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
        await session_a.rollback()

    async def create_after_fix(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _panel_user(username)
        return panel_users[username]

    client.create_user = AsyncMock(side_effect=create_after_fix)
    async with factory() as session_b:
        result = await issue_free_trial(
            session_b,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=50,
            duration_days=10,
            hwid_limit=4,
            group_ids=[8],
        )
        await session_b.commit()
        assert result.order.trial_kind == "personal"
        assert result.order.status is OrderStatus.ACTIVE
        assert result.order.delivery_group_ids == [7]
        assert (
            await session_b.scalar(
                select(func.count()).select_from(Order).where(Order.user_id == user_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_active_personal_trial_repeat_does_not_touch_the_panel(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """An ACTIVE personal trial is not created again. The panel is only read."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=703)
    panel_users: dict[str, UserResponse] = {}

    async def create(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _panel_user(username)
        return panel_users[username]

    client = _client(panel_users=panel_users, create=create)
    async with factory() as session_a:
        first = await issue_free_trial(
            session_a,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=5,
            duration_days=1,
            hwid_limit=1,
            group_ids=[7],
        )
        await session_a.commit()

    async def get_existing(username: str) -> UserResponse:
        return panel_users[username]

    client.get_user = AsyncMock(side_effect=get_existing)
    client.create_user = AsyncMock(side_effect=AssertionError("ACTIVE repeat must not create"))
    async with factory() as session_b:
        repeated = await issue_free_trial(
            session_b,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=50,
            duration_days=10,
            hwid_limit=4,
            group_ids=[8],
        )
        assert repeated.already_claimed is True
        assert repeated.order.id == first.order.id
        assert repeated.account.id == first.account.id
    client.get_user.assert_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_or_unidentified_claimed_trial_history_fails_before_panel_io(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Recovery must never guess which legacy/ambiguous trial is personal."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=704)
    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.trial_claimed_at = datetime.now(UTC)
        plan = Plan(
            name="trial-5GB-1d",
            description="test",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add_all(
            [
                Order(
                    user_id=user_id,
                    plan_id=plan.id,
                    status=OrderStatus.PROVISIONING,
                    price_toman=0,
                    discount_toman=0,
                    panel_username="ambiguous_one",
                ),
                Order(
                    user_id=user_id,
                    plan_id=plan.id,
                    status=OrderStatus.PROVISION_FAILED,
                    price_toman=0,
                    discount_toman=0,
                    panel_username="ambiguous_two",
                ),
            ]
        )
        await session.commit()

    client = _client(panel_users={}, create=AssertionError("unsafe history must not create"))
    async with factory() as session:
        with pytest.raises(TrialError, match="trial_recovery_unsafe"):
            await issue_free_trial(
                session,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_shape_paid_account_does_not_renew_when_personal_trial_is_created(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Without force_new, a paid 5GB/1d account would silently be extended."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=705)
    async with factory() as session:
        paid_plan = Plan(
            name="paid-5GB-1d",
            description="paid",
            data_limit_gb=5,
            duration_days=1,
            price_toman=10_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add(paid_plan)
        await session.flush()
        paid_order = Order(
            user_id=user_id,
            plan_id=paid_plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=10_000,
            discount_toman=0,
            panel_username="paid_5g_1d",
        )
        paid_account = PasarGuardAccount(
            user_id=user_id,
            panel_user_id=77,
            panel_username="paid_5g_1d",
            plan_fingerprint="volume:5:1",
        )
        session.add_all([paid_order, paid_account])
        await session.commit()

    panel_users: dict[str, UserResponse] = {}

    async def create(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _panel_user(username, panel_id=78)
        return panel_users[username]

    client = _client(panel_users=panel_users, create=create)
    async with factory() as session:
        result = await issue_free_trial(
            session,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=5,
            duration_days=1,
            hwid_limit=1,
            group_ids=[7],
        )
        await session.commit()
        paid = await session.scalar(
            select(PasarGuardAccount).where(PasarGuardAccount.panel_username == "paid_5g_1d")
        )
        assert result.order.force_new is True
        assert result.account.panel_username != "paid_5g_1d"
        assert paid is not None and paid.plan_fingerprint == "volume:5:1"


@pytest.mark.asyncio
async def test_null_kind_legacy_trial_history_safely_denies_new_personal_claim(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A legacy unknown operation must not become invisible and mint a second trial."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=706)
    async with factory() as session:
        plan = Plan(
            name="trial-5GB-1d",
            description="legacy",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                status=OrderStatus.PROVISION_FAILED,
                price_toman=0,
                discount_toman=0,
                panel_username="legacy_unknown",
                trial_kind=None,
            )
        )
        await session.commit()

    client = _client(panel_users={}, create=AssertionError("unknown history must not create"))
    async with factory() as session:
        with pytest.raises(TrialError, match="trial_recovery_unsafe"):
            await issue_free_trial(
                session,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_personal_trial_with_missing_matching_account_fails_without_fallback(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A newer paid account cannot be leaked as the account for a broken personal trial."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=707)
    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.trial_claimed_at = datetime.now(UTC)
        plan = Plan(
            name="trial-5GB-1d",
            description="trial",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                discount_toman=0,
                panel_username="trial_missing",
                trial_kind="personal",
            )
        )
        session.add(
            PasarGuardAccount(
                user_id=user_id,
                panel_user_id=88,
                panel_username="newer_paid",
                plan_fingerprint="volume:5:1",
            )
        )
        await session.commit()

    client = _client(panel_users={}, create=AssertionError("broken ACTIVE must not create"))
    async with factory() as session:
        with pytest.raises(TrialError, match="trial_recovery_unsafe"):
            await issue_free_trial(
                session,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfilled_active_personal_trial_without_snapshot_returns_exact_account(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A proven historical ACTIVE trial is returned as-is once the panel still has it."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=710)
    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.trial_claimed_at = datetime.now(UTC)
        plan = Plan(
            name="trial-5GB-1d",
            description="backfilled",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                discount_toman=0,
                panel_username="backfilled_active",
                trial_kind="personal",
            )
        )
        session.add(
            PasarGuardAccount(
                user_id=user_id,
                panel_user_id=91,
                panel_username="backfilled_active",
                plan_fingerprint="trial:5:1",
            )
        )
        await session.commit()

    client = _client(
        panel_users={"backfilled_active": _panel_user("backfilled_active", panel_id=91)},
        create=AssertionError("ACTIVE repeat must not create"),
    )
    async with factory() as session:
        result = await issue_free_trial(
            session,
            client,
            user_id=user_id,
            category_slug="unlimited",
            counts_legacy=True,
            data_limit_gb=50,
            duration_days=10,
            hwid_limit=4,
            group_ids=[7],
        )
        assert result.already_claimed is True
        assert result.account.panel_username == "backfilled_active"
    client.get_user.assert_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_recovery_requires_all_delivery_snapshots(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A legacy personal row without its promise cannot safely be reprovisioned."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=709)
    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.trial_claimed_at = datetime.now(UTC)
        plan = Plan(
            name="trial-5GB-1d",
            description="trial",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=user_id,
                plan_id=plan.id,
                status=OrderStatus.PROVISION_FAILED,
                price_toman=0,
                discount_toman=0,
                panel_username="incomplete_snapshot",
                trial_kind="personal",
                delivery_data_limit_gb=5,
                delivery_duration_days=1,
                delivery_hwid_limit=None,
                delivery_group_ids=[7],
            )
        )
        await session.commit()

    client = _client(panel_users={}, create=AssertionError("incomplete recovery must not create"))
    async with factory() as session:
        with pytest.raises(TrialError, match="trial_recovery_unsafe"):
            await issue_free_trial(
                session,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_trial_unique_collision_leaves_the_session_usable(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An insert collision must be savepoint-isolated, not poison the caller transaction."""
    factory, _url = two_sessions
    user_id = await _seed_user(factory, telegram_id=708)
    async with factory() as session:
        from sqlalchemy.exc import IntegrityError

        original_flush = session.flush

        async def collision_when_personal_order_is_pending(*args: Any, **kwargs: Any) -> None:
            if any(getattr(item, "trial_kind", None) == "personal" for item in session.new):
                raise IntegrityError("insert orders", {}, RuntimeError("personal collision"))
            await original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", collision_when_personal_order_is_pending)
        client = _client(
            panel_users={}, create=AssertionError("collision must happen before panel IO")
        )
        with pytest.raises(TrialError, match="trial_creation_conflict"):
            await issue_free_trial(
                session,
                client,
                user_id=user_id,
                category_slug="unlimited",
                counts_legacy=True,
                data_limit_gb=5,
                duration_days=1,
                hwid_limit=1,
                group_ids=[7],
            )
        assert await session.scalar(select(User.id).where(User.id == user_id)) == user_id
    client.get_user.assert_not_awaited()
    client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_handler_uses_recovered_order_snapshot_for_delivery_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed settings must not tell a recovered customer the wrong trial shape."""
    from bot.handlers import referral

    order = SimpleNamespace(id=80, plan_id=0, delivery_data_limit_gb=5, delivery_duration_days=1)
    account = SimpleNamespace(id=90, subscription_url="https://panel.example.com/sub/trial")
    result = TrialResult(order=order, account=account, already_claimed=False)
    session = AsyncMock()
    message = SimpleNamespace(answer=AsyncMock())
    settings = SimpleNamespace(trial_enabled=True)
    user = SimpleNamespace(id=70, is_reseller=False)
    client = AsyncMock()
    delivery = AsyncMock()
    # Today's group trial is 50 GB / 10 days; the recovered order was 5 / 1.
    offer = TrialOffer(
        slug="unlimited",
        title_fa="نامحدود",
        data_limit_gb=50,
        duration_days=10,
        group_ids=[9],
        counts_legacy=True,
    )
    monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=[offer]))
    monkeypatch.setattr(referral, "get_trial_hwid_limit", AsyncMock(return_value=4))
    monkeypatch.setattr(referral, "get_pasarguard_client", AsyncMock(return_value=client))
    monkeypatch.setattr(referral, "issue_free_trial", AsyncMock(return_value=result))
    monkeypatch.setattr(referral, "deliver_provisioned_service_card", delivery)

    await referral.render_trial(message, session, settings, user, AsyncMock())

    assert delivery.await_args.kwargs["intro"] == (
        "🎟 تست رایگان فعال شد (۱ روز / ۵ گیگ)\n📂 گروه: نامحدود"
    )
