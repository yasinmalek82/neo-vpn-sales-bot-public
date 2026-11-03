"""Durability findings. One open order must hold, and a committed send is not replayed."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from database.models import Order, OrderStatus, Plan, User
from database.models.broadcast import (
    BroadcastAudience,
)
from database.models.pasarguard_account import PasarGuardAccount
from database.models.plan import DataLimitResetStrategy
from database.models.report import REPORT_SETTINGS_ROW_ID, ReportSettings
from services.broadcast import create_broadcast, run_pending_broadcasts
from services.orders import create_pending_order
from services.payments import approve_payment, submit_card_receipt
from services.pricing import PriceResult
from services.provisioning import provision_order
from services.report_outbox import enqueue_report, flush_reports
from services.service_ops import add_data, extend_days
from tests.integration.durability import panel_accepts_then_loses_reply

GB = 1024**3
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_ids = itertools.count(100)


def _panel_user(username: str, *, user_id: int | None = None, **kwargs: Any) -> UserResponse:
    return UserResponse(
        id=next(_ids) if user_id is None else user_id,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=int(kwargs.get("used_traffic", 0)),
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=kwargs.get("data_limit", 10 * GB),
        expire=kwargs.get("expire"),
    )


async def _seed_approved_order(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Commit a PAYMENT_APPROVED order; return its id."""
    async with factory() as session:
        customer = User(
            telegram_id=1001,
            username="cust",
            first_name="C",
            last_name=None,
            language_code="fa",
            referral_code="CUST0001",
            is_admin=False,
        )
        admin = User(
            telegram_id=9001,
            username="admin",
            first_name="A",
            last_name=None,
            language_code="fa",
            referral_code="ADMIN001",
            is_admin=True,
        )
        plan = Plan(
            name="volume-10GB-30d",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=150_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1, 2],
            hwid_limit=2,
            is_active=True,
        )
        session.add_all([customer, admin, plan])
        await session.flush()
        order = Order(
            user_id=customer.id,
            plan_id=plan.id,
            status=OrderStatus.PENDING_PAYMENT,
            price_toman=150_000,
            discount_toman=0,
        )
        session.add(order)
        await session.flush()
        await submit_card_receipt(
            session,
            order_id=order.id,
            user_id=customer.id,
            receipt_file_id="file",
            card_number_display="6037",
        )
        await approve_payment(session, order_id=order.id, admin_user_id=admin.id)
        await session.commit()
        return int(order.id)


# --------------------------------------------------------------------------- #
# SH-01
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sh01_create_intent_does_not_survive_rollback(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """After a lost create_user reply and a rollback, retry must reuse the name."""
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)

    created_names: list[str] = []

    async def create_user(payload: Any) -> UserResponse:
        # Panel accepted the create; the reply never came back.
        created_names.append(str(payload.username))
        await panel_accepts_then_loses_reply()
        raise AssertionError("unreachable")  # pragma: no cover

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=create_user)
    client.subscription_url_from_response = lambda u: u.subscription_url

    async with factory() as session_a:
        with pytest.raises(TimeoutError):
            await provision_order(session_a, client, order_id=order_id, default_group_ids=[9])
        await session_a.rollback()

    assert len(created_names) == 1
    first_name = created_names[0]

    # Fresh session — whatever survived the crash/rollback is what the retry sees.
    async def create_user_ok(payload: Any) -> UserResponse:
        created_names.append(str(payload.username))
        return _panel_user(str(payload.username))

    client.create_user = AsyncMock(side_effect=create_user_ok)
    async with factory() as session_b:
        await provision_order(session_b, client, order_id=order_id, default_group_ids=[9])
        await session_b.commit()

    assert len(created_names) == 2
    assert (
        created_names[1] == first_name
    ), f"retry asked for {created_names[1]!r}, expected the original {first_name!r}"


@pytest.mark.asyncio
async def test_sh01_a_lost_reply_then_restart_reuses_the_same_panel_account(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Restart (fresh session, same DB) must find the panel user already created."""
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)

    panel_users: dict[str, UserResponse] = {}
    create_calls = 0
    get_hits_on_existing = 0

    async def get_user(username: str) -> UserResponse:
        nonlocal get_hits_on_existing
        if username in panel_users:
            get_hits_on_existing += 1
            return panel_users[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    async def create_user(payload: Any) -> UserResponse:
        nonlocal create_calls
        create_calls += 1
        name = str(payload.username)
        panel_users[name] = _panel_user(name)
        # Panel accepted; the reply never came back.
        await panel_accepts_then_loses_reply()
        raise AssertionError("unreachable")  # pragma: no cover

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=create_user)
    client.subscription_url_from_response = lambda u: u.subscription_url

    async with factory() as session_a:
        with pytest.raises(TimeoutError):
            await provision_order(session_a, client, order_id=order_id, default_group_ids=[9])
        # No explicit rollback — simulate process death after the durable stamp.
        # Leaving the context rolls back only uncommitted work (e.g. PROVISION_FAILED).

    assert create_calls == 1
    assert len(panel_users) == 1

    # Restart: fresh session on the same on-disk DB.
    async with factory() as session_b:
        await provision_order(session_b, client, order_id=order_id, default_group_ids=[9])
        await session_b.commit()

    assert create_calls == 1, "create_user must not run again after a restart"
    assert get_hits_on_existing >= 1, "second pass must find the existing panel user via get_user"


# --------------------------------------------------------------------------- #
# SH-02
# --------------------------------------------------------------------------- #


class _ApplyThenTimeoutPanel:
    """Panel that applies extend/add-data, then loses the reply."""

    def __init__(self, *, expire: int, data_limit: int) -> None:
        self.expire = expire
        self.data_limit = data_limit
        self.used = 0
        self.status = "active"
        self.extend_calls = 0
        self.add_calls = 0

    async def get_user(self, username: str) -> SimpleNamespace:
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value=self.status),
            expire=self.expire,
            data_limit=self.data_limit,
            used_traffic=self.used,
        )

    async def extend_subscription(
        self, username: str, *, expire: int, data_limit: int | None = None
    ) -> SimpleNamespace:
        _ = username, data_limit
        self.extend_calls += 1
        self.expire = int(expire)  # applied
        await panel_accepts_then_loses_reply()
        raise AssertionError("unreachable")  # pragma: no cover

    async def increase_data_limit(self, username: str, data_limit: int) -> SimpleNamespace:
        _ = username
        self.add_calls += 1
        self.data_limit = int(data_limit)  # applied
        await panel_accepts_then_loses_reply()
        raise AssertionError("unreachable")  # pragma: no cover


async def _live_account(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as session:
        user = User(
            telegram_id=7,
            username="u",
            first_name="U",
            last_name=None,
            language_code="fa",
            referral_code="LIVE0001",
            is_admin=False,
        )
        session.add(user)
        await session.flush()
        account = PasarGuardAccount(user_id=user.id, panel_username="yasin_ab12", panel_user_id=7)
        session.add(account)
        await session.commit()
        return int(account.id)


@pytest.mark.asyncio
async def test_sh02_extend_days_double_applies_on_retry(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    base_expire = int((NOW + timedelta(days=10)).timestamp())
    panel = _ApplyThenTimeoutPanel(expire=base_expire, data_limit=50 * GB)

    async with factory() as session:
        with pytest.raises(TimeoutError):
            await extend_days(session, panel, account_id=account_id, days=7, now=NOW)  # type: ignore[arg-type]
        # Retry the same operator command after the lost reply.
        with pytest.raises(TimeoutError):
            await extend_days(session, panel, account_id=account_id, days=7, now=NOW)  # type: ignore[arg-type]

    expected = base_expire + 7 * 86_400
    assert (
        panel.expire == expected
    ), f"expiry moved to +{(panel.expire - base_expire) // 86_400} days, expected 7"
    assert panel.extend_calls == 2


@pytest.mark.asyncio
async def test_sh02_add_data_double_applies_on_retry(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    base_limit = 50 * GB
    panel = _ApplyThenTimeoutPanel(
        expire=int((NOW + timedelta(days=10)).timestamp()), data_limit=base_limit
    )

    async with factory() as session:
        with pytest.raises(TimeoutError):
            await add_data(session, panel, account_id=account_id, gigabytes=7)  # type: ignore[arg-type]
        with pytest.raises(TimeoutError):
            await add_data(session, panel, account_id=account_id, gigabytes=7)  # type: ignore[arg-type]

    expected = base_limit + 7 * GB
    assert (
        panel.data_limit == expected
    ), f"data limit grew by {(panel.data_limit - base_limit) // GB} GB, expected 7"
    assert panel.add_calls == 2


# --------------------------------------------------------------------------- #
# SH-02 — deliberate second command (not a retry)
# --------------------------------------------------------------------------- #


class _ApplyOkPanel:
    """Panel that applies extend/add-data and returns successfully."""

    def __init__(self, *, expire: int, data_limit: int) -> None:
        self.expire = expire
        self.data_limit = data_limit
        self.used = 0
        self.status = "active"
        self.extend_calls = 0

    async def get_user(self, username: str) -> SimpleNamespace:
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value=self.status),
            expire=self.expire,
            data_limit=self.data_limit,
            used_traffic=self.used,
        )

    async def extend_subscription(
        self, username: str, *, expire: int, data_limit: int | None = None
    ) -> SimpleNamespace:
        _ = username, data_limit
        self.extend_calls += 1
        self.expire = int(expire)
        return await self.get_user(username)

    async def increase_data_limit(self, username: str, data_limit: int) -> SimpleNamespace:
        _ = username
        self.data_limit = int(data_limit)
        return await self.get_user(username)


@pytest.mark.asyncio
async def test_sh02_a_second_operator_command_is_not_swallowed_as_a_retry(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """After an operation is applied, a new extend_days(7) must move expiry again."""
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    base_expire = int((NOW + timedelta(days=10)).timestamp())
    panel = _ApplyOkPanel(expire=base_expire, data_limit=50 * GB)

    async with factory() as session:
        first = await extend_days(session, panel, account_id=account_id, days=7, now=NOW)  # type: ignore[arg-type]
        second = await extend_days(session, panel, account_id=account_id, days=7, now=NOW)  # type: ignore[arg-type]

    assert first.expire_unix == base_expire + 7 * 86_400
    assert second.expire_unix == base_expire + 14 * 86_400
    assert panel.expire == base_expire + 14 * 86_400


@pytest.mark.asyncio
async def test_sh02_replay_of_an_applied_operation_does_not_extend_again(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """The same caller identity after APPLIED is a replay, not a second command."""
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    base_expire = int((NOW + timedelta(days=10)).timestamp())
    panel = _ApplyOkPanel(expire=base_expire, data_limit=50 * GB)

    async with factory() as session:
        await extend_days(
            session,
            panel,  # type: ignore[arg-type]
            account_id=account_id,
            days=7,
            now=NOW,
            operation_key="extend-once",
        )
        await extend_days(
            session,
            panel,  # type: ignore[arg-type]
            account_id=account_id,
            days=7,
            now=NOW,
            operation_key="extend-once",
        )

    assert panel.expire == base_expire + 7 * 86_400
    assert panel.extend_calls == 1


# --------------------------------------------------------------------------- #
# SH-05
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sh05_two_open_orders_can_be_created_concurrently(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Two direct create_pending_order calls must not both survive."""
    factory, _url = two_sessions
    price = PriceResult(
        original_price=100_000,
        discount_amount=0,
        final_price=100_000,
        currency="IRT",
        meta={},
    )

    async with factory() as session:
        user = User(
            telegram_id=55,
            username="buyer",
            first_name="B",
            last_name=None,
            language_code="fa",
            referral_code="BUYER001",
            is_admin=False,
        )
        plan = Plan(
            name="volume-10GB-30d",
            description="t",
            data_limit_gb=10,
            duration_days=30,
            price_toman=100_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add_all([user, plan])
        await session.commit()
        user_id = int(user.id)
        plan_id = int(plan.id)

    async with factory() as s1:
        plan1 = await s1.get(Plan, plan_id)
        assert plan1 is not None
        await create_pending_order(s1, user_id=user_id, plan=plan1, price=price)
        await s1.commit()

    async with factory() as s2:
        plan2 = await s2.get(Plan, plan_id)
        assert plan2 is not None
        await create_pending_order(s2, user_id=user_id, plan=plan2, price=price)
        await s2.commit()

    async with factory() as session:
        open_count = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == user_id,
                Order.status.in_((OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW)),
            )
        )
    assert int(open_count or 0) == 1, f"open orders={open_count}, expected exactly 1"


# --------------------------------------------------------------------------- #
# SH-09
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sh09_a_crash_after_send_does_not_replay_a_broadcast(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash after Telegram accepts the send must not re-deliver on restart."""
    factory, _url = two_sessions
    monkeypatch.setattr("services.broadcast.SEND_INTERVAL_SECONDS", 0)

    async with factory() as session:
        users = [
            User(
                telegram_id=10_000 + i,
                username=f"c{i}",
                first_name=f"C{i}",
                last_name=None,
                language_code="fa",
                referral_code=f"BC{i:06d}",
                is_admin=False,
            )
            for i in range(2)
        ]
        session.add_all(users)
        await session.commit()
        await create_broadcast(
            session,
            body="سلام — پیام تست",
            audience=BroadcastAudience.all_customers,
            admin_telegram_id=1,
        )
        await session.commit()

    sent_to: list[int] = []

    class _Bot:
        async def send_message(self, *, chat_id: int, text: str) -> SimpleNamespace:
            _ = text
            sent_to.append(int(chat_id))
            return SimpleNamespace(message_id=len(sent_to))

    # First worker pass: deliver, then crash before the state commit.
    async with factory() as session:
        orig_commit = session.commit

        async def crash_after_sends() -> None:
            raise RuntimeError("crash after send, before state commit")

        # Replace only the final batch commit inside run_pending_broadcasts.
        session.commit = crash_after_sends  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="crash after send"):
            await run_pending_broadcasts(session, _Bot(), batch=40)  # type: ignore[arg-type]
        session.commit = orig_commit  # type: ignore[method-assign]
        await session.rollback()

    assert len(sent_to) == 2, "first pass should have handed both messages to Telegram"

    # Restart the worker with a fresh session — must not send again.
    async with factory() as session:
        await run_pending_broadcasts(session, _Bot(), batch=40)  # type: ignore[arg-type]
        await session.commit()

    # Correct: each recipient once. A crash of the batch commit must not replay.
    from collections import Counter

    counts = Counter(sent_to)
    assert all(
        n == 1 for n in counts.values()
    ), f"replay detected: send counts per chat_id={dict(counts)}"


@pytest.mark.asyncio
async def test_sh09_a_crash_after_send_does_not_replay_a_report(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """A crash after a report is accepted must not send that report again."""
    factory, _url = two_sessions

    async with factory() as session:
        session.add(
            ReportSettings(
                id=REPORT_SETTINGS_ROW_ID,
                chat_id=-100,
                chat_title="reports",
                topics={"sales": 11},
                verified={},
                enabled=True,
            )
        )
        await session.commit()
        await enqueue_report(session, event_key="e1", topic_key="sales", body="یک")
        await enqueue_report(session, event_key="e2", topic_key="sales", body="دو")
        await session.commit()

    sent: list[str] = []

    class _Bot:
        async def send_message(
            self,
            *,
            chat_id: int,
            text: str,
            message_thread_id: int | None = None,
        ) -> SimpleNamespace:
            _ = (chat_id, message_thread_id)
            sent.append(text)
            return SimpleNamespace(message_id=len(sent))

    async with factory() as session:
        orig_commit = session.commit

        async def crash_after_sends() -> None:
            raise RuntimeError("crash after send, before state commit")

        session.commit = crash_after_sends  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="crash after send"):
            await flush_reports(session, _Bot())  # type: ignore[arg-type]
        session.commit = orig_commit  # type: ignore[method-assign]
        await session.rollback()

    assert len(sent) == 2, "first pass should have handed both reports to Telegram"

    async with factory() as session:
        await flush_reports(session, _Bot())  # type: ignore[arg-type]
        await session.commit()

    from collections import Counter

    counts = Counter(sent)
    assert all(n == 1 for n in counts.values()), f"replay detected: {dict(counts)}"
