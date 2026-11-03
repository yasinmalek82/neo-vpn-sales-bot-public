"""A scheduler job committing during the panel call must not cost the customer the service.

``provision_order`` commits its intent, re-reads the order, calls the panel,
then writes. The re-read pinned a WAL read snapshot, so any commit on another
connection during the panel call made the write fail at once («database is
locked», SQLITE_BUSY_SNAPSHOT; ``busy_timeout`` does not apply). The panel had
already created or extended the service; the customer who paid got «یه مشکلی
پیش اومد» and no card until someone retried.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardNotFoundError
from database.models import Order, OrderStatus, User
from database.models.pasarguard_account import PasarGuardAccount
from database.models.report import ReportOutbox
from services.provisioning import ProvisionError, plan_fingerprint, provision_order
from tests.integration.test_sh_durability import _seed_approved_order

GB = 1024**3
Factory = async_sessionmaker[AsyncSession]


async def _scheduler_commits(factory: Factory) -> None:
    """What ``run_report_flush`` / the broadcast worker do: commit on another connection."""
    async with factory() as other:
        row = await other.scalar(select(User).where(User.telegram_id == 9001))
        row.first_name = f"A {datetime.now(UTC).timestamp()}"
        await other.commit()


class RacingPanel:
    """A panel whose every call lasts long enough for something else to commit."""

    def __init__(
        self, during_call: Callable[[], Awaitable[None]], *, only: str | None = None
    ) -> None:
        self._race = during_call
        self._only = only
        self.users: dict[str, UserResponse] = {}
        self.refuse_create = False
        self.created = 0
        self.modified = 0

    async def _during_call(self, name: str) -> None:
        if self._only in (None, name):
            await self._race()

    @staticmethod
    def subscription_url_from_response(user: UserResponse) -> str | None:
        return user.subscription_url

    def _user(self, username: str, **values: Any) -> UserResponse:
        return UserResponse(
            id=len(self.users) + 50,
            username=username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=f"https://panel.example.com/sub/{username}",
            data_limit=values.get("data_limit", 10 * GB),
            expire=values.get("expire"),
            hwid_limit=values.get("hwid_limit"),
        )

    async def get_user(self, username: str) -> UserResponse:
        await self._during_call("get_user")
        if username not in self.users:
            raise PasarGuardNotFoundError("missing", status_code=404)
        return self.users[username]

    async def create_user(self, payload: Any) -> UserResponse:
        await self._during_call("create_user")
        if self.refuse_create:
            raise PasarGuardAPIError("group does not exist", status_code=400)
        self.created += 1
        self.users[payload.username] = self._user(
            payload.username,
            data_limit=payload.data_limit,
            expire=payload.expire,
            hwid_limit=payload.hwid_limit,
        )
        return self.users[payload.username]

    async def modify_user(self, username: str, payload: Any) -> UserResponse:
        await self._during_call("modify_user")
        self.modified += 1
        self.users[username] = self._user(
            username,
            data_limit=payload.data_limit,
            expire=payload.expire,
            hwid_limit=payload.hwid_limit,
        )
        return self.users[username]


async def _order(factory: Factory, order_id: int) -> Order:
    async with factory() as check:
        order = await check.get(Order, order_id)
        assert order is not None
        return order


async def _seed_renewal_of_a_live_service(factory: Factory, panel: RacingPanel) -> int:
    """The approved order renews a service the customer already has on the panel."""
    order_id = await _seed_approved_order(factory)
    expire = int((datetime.now(UTC) + timedelta(days=5)).timestamp())
    panel.users["cust_ab12"] = panel._user("cust_ab12", expire=expire)
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        plan = await session.get(type(order).plan.property.mapper.class_, order.plan_id)
        account = PasarGuardAccount(
            user_id=order.user_id,
            panel_username="cust_ab12",
            panel_user_id=50,
            plan_fingerprint=plan_fingerprint(plan),
        )
        session.add(account)
        await session.flush()
        order.renew_account_id = account.id
        await session.commit()
    return order_id


@pytest.mark.asyncio
async def test_a_job_committing_during_create_user_does_not_lose_the_paid_service(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)
    panel = RacingPanel(lambda: _scheduler_commits(factory))

    async with factory() as session:
        account, _reward = await provision_order(
            session, panel, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
        )
        await session.commit()

    order = await _order(factory, order_id)
    assert order.status is OrderStatus.ACTIVE
    assert account.panel_username == order.panel_username
    assert panel.created == 1


@pytest.mark.asyncio
async def test_a_job_committing_during_a_resumed_create_does_not_lose_the_paid_service(
    two_sessions: tuple[Factory, str],
) -> None:
    """The retry of an order left PROVISIONING: nothing is written before the panel call."""
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)

    async def nothing() -> None:
        return None

    lost = RacingPanel(nothing)

    async def lose_reply(payload: Any) -> UserResponse:
        await RacingPanel.create_user(lost, payload)
        raise TimeoutError("reply lost after the panel created the user")

    lost.create_user = lose_reply  # type: ignore[method-assign]
    async with factory() as session:
        with pytest.raises(TimeoutError):
            await provision_order(
                session, lost, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
            )
        await session.rollback()
    assert (await _order(factory, order_id)).status is OrderStatus.PROVISIONING

    panel = RacingPanel(lambda: _scheduler_commits(factory))
    panel.users = lost.users
    async with factory() as session:
        await provision_order(
            session, panel, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
        )
        await session.commit()

    assert (await _order(factory, order_id)).status is OrderStatus.ACTIVE
    assert panel.created == 0, "the retry found the user the first attempt created"


@pytest.mark.asyncio
async def test_a_job_committing_during_a_renewal_does_not_lose_the_paid_extension(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    # Not during ``get_user``: the order's move to PROVISIONING is still
    # uncommitted there, so this session holds the write lock and no job can
    # commit. The window is the ``modify_user`` that follows the intent commit.
    panel = RacingPanel(lambda: _scheduler_commits(factory), only="modify_user")
    order_id = await _seed_renewal_of_a_live_service(factory, panel)

    async with factory() as session:
        await provision_order(session, panel, order_id=order_id)  # type: ignore[arg-type]
        await session.commit()

    order = await _order(factory, order_id)
    assert order.status is OrderStatus.ACTIVE
    assert order.panel_username == "cust_ab12"
    assert panel.modified == 1 and panel.created == 0


@pytest.mark.asyncio
async def test_a_job_committing_during_a_refused_create_still_records_the_failure(
    two_sessions: tuple[Factory, str],
) -> None:
    """The refund and the «نیاز به رسیدگی» report both hang on PROVISION_FAILED being saved."""
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)
    panel = RacingPanel(lambda: _scheduler_commits(factory))
    panel.refuse_create = True

    async with factory() as session:
        with pytest.raises(PasarGuardAPIError):
            await provision_order(
                session, panel, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
            )
        await session.commit()  # what the callers do before they settle the failure

    assert (await _order(factory, order_id)).status is OrderStatus.PROVISION_FAILED
    async with factory() as check:
        reports = (await check.scalars(select(ReportOutbox))).all()
    assert any(f"{order_id}" in r.event_key for r in reports)


@pytest.mark.asyncio
async def test_an_order_that_changed_during_the_panel_call_is_not_overwritten(
    two_sessions: tuple[Factory, str],
) -> None:
    """Ending the stale snapshot must not turn into «write anyway»: the order is
    re-read after the panel call, and one that is no longer PROVISIONING is left
    exactly as the other writer made it."""
    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)

    async def someone_else_fails_the_order() -> None:
        async with factory() as other:
            order = await other.get(Order, order_id)
            assert order is not None
            if order.status is OrderStatus.PROVISIONING:
                order.status = OrderStatus.PROVISION_FAILED
                await other.commit()

    panel = RacingPanel(someone_else_fails_the_order)
    async with factory() as session:
        with pytest.raises(ProvisionError, match="order_changed"):
            await provision_order(
                session, panel, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
            )
        await session.rollback()

    assert (await _order(factory, order_id)).status is OrderStatus.PROVISION_FAILED
    async with factory() as check:
        assert (await check.scalars(select(PasarGuardAccount))).first() is None


async def _set(factory: Factory, order_id: int, **values: Any) -> None:
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        for name, value in values.items():
            setattr(order, name, value)
        await session.commit()


@pytest.mark.asyncio
async def test_a_job_committing_during_a_resumed_renewal_read_does_not_fail_the_intent(
    two_sessions: tuple[Factory, str],
) -> None:
    """A renewal left PROVISIONING before its targets were stored: the retry
    writes nothing before the live read, so that read must not pin a snapshot."""
    factory, _url = two_sessions
    panel = RacingPanel(lambda: _scheduler_commits(factory), only="get_user")
    order_id = await _seed_renewal_of_a_live_service(factory, panel)
    await _set(factory, order_id, status=OrderStatus.PROVISIONING)

    async with factory() as session:
        await provision_order(session, panel, order_id=order_id)  # type: ignore[arg-type]
        await session.commit()

    assert (await _order(factory, order_id)).status is OrderStatus.ACTIVE
    assert panel.modified == 1


@pytest.mark.asyncio
async def test_a_job_committing_during_a_renewal_reconcile_does_not_lose_the_paid_extension(
    two_sessions: tuple[Factory, str],
) -> None:
    """The retry after a lost ``modify_user`` reply: targets are on file, the
    panel is already extended, and only the local finalise is left."""
    factory, _url = two_sessions

    async def nothing() -> None:
        return None

    lost = RacingPanel(nothing)
    order_id = await _seed_renewal_of_a_live_service(factory, lost)

    async def lose_reply(username: str, payload: Any) -> UserResponse:
        await RacingPanel.modify_user(lost, username, payload)
        raise TimeoutError("reply lost after the panel extended the user")

    lost.modify_user = lose_reply  # type: ignore[method-assign]
    async with factory() as session:
        with pytest.raises(ProvisionError, match="panel_transient"):
            await provision_order(session, lost, order_id=order_id)  # type: ignore[arg-type]
        await session.rollback()
    assert (await _order(factory, order_id)).renew_target_expire_unix is not None

    panel = RacingPanel(lambda: _scheduler_commits(factory))
    panel.users = lost.users
    async with factory() as session:
        await provision_order(session, panel, order_id=order_id)  # type: ignore[arg-type]
        await session.commit()

    assert (await _order(factory, order_id)).status is OrderStatus.ACTIVE
    assert panel.modified == 0, "the panel already held the target: nothing is extended twice"


@pytest.mark.asyncio
async def test_a_job_committing_during_a_plan_switch_does_not_lose_the_paid_switch(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    panel = RacingPanel(lambda: _scheduler_commits(factory))
    order_id = await _seed_renewal_of_a_live_service(factory, panel)
    await _set(factory, order_id, renew_resets=True)
    resets: list[str] = []

    async def reset_user_data_usage(username: str) -> UserResponse:
        resets.append(username)
        return panel.users[username]

    panel.reset_user_data_usage = reset_user_data_usage  # type: ignore[attr-defined]
    async with factory() as session:
        await provision_order(
            session, panel, order_id=order_id, default_group_ids=[9]  # type: ignore[arg-type]
        )
        await session.commit()

    order = await _order(factory, order_id)
    assert order.status is OrderStatus.ACTIVE
    assert order.panel_username == "cust_ab12"
    assert resets == ["cust_ab12"] and panel.modified == 1
