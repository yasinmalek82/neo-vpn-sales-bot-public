"""A job committing during a panel call must not fail the write that follows it.

Same class as the report-group attach: read (a WAL read snapshot), await the
panel, write. Another connection's commit during the await makes SQLite refuse
the write at once («database is locked», SQLITE_BUSY_SNAPSHOT).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models.panel_node_state import PanelNodeState
from database.models.pasarguard_account import PasarGuardAccount
from database.models.service_operation import ServiceOperation
from services.panel_watch import check_panel_and_nodes
from services.service_ops import ServiceOpError, add_data, extend_days, revoke_link
from tests.integration.test_sh_durability import _ApplyOkPanel, _live_account
from tests.unit.test_panel_watch import FakeClient

GB = 1024**3
Factory = async_sessionmaker[AsyncSession]
Race = Callable[[], Awaitable[None]]


async def _scheduler_commits(factory: Factory) -> None:
    """What ``run_report_flush`` / the broadcast worker do: commit on another connection."""
    async with factory() as other:
        row = await other.scalar(select(PasarGuardAccount))
        row.note = f"touched {datetime.now(UTC).timestamp()}"
        await other.commit()


class _SlowReadPanel(_ApplyOkPanel):
    """The live read lasts long enough for something else to commit."""

    def __init__(self, during_read: Race, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._during_read: Race | None = during_read

    async def get_user(self, username: str) -> SimpleNamespace:
        if self._during_read is not None:
            race, self._during_read = self._during_read, None
            await race()
        return await super().get_user(username)


def _in(days: int) -> int:
    return int((datetime.now(UTC) + timedelta(days=days)).timestamp())


@pytest.mark.asyncio
async def test_a_job_committing_during_the_live_read_does_not_fail_an_extension(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    panel = _SlowReadPanel(lambda: _scheduler_commits(factory), expire=_in(5), data_limit=GB)

    async with factory() as session:
        await extend_days(session, panel, account_id=account_id, days=30)  # type: ignore[arg-type]

    assert panel.extend_calls == 1
    async with factory() as check:
        op = await check.scalar(select(ServiceOperation))
    assert op is not None and op.state == "applied"


@pytest.mark.asyncio
async def test_a_job_committing_during_the_live_read_does_not_fail_added_data(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    account_id = await _live_account(factory)
    panel = _SlowReadPanel(lambda: _scheduler_commits(factory), expire=_in(5), data_limit=GB)

    async with factory() as session:
        await add_data(session, panel, account_id=account_id, gigabytes=5)  # type: ignore[arg-type]

    assert panel.data_limit == 6 * GB


@pytest.mark.asyncio
async def test_a_second_operator_starting_during_the_live_read_is_not_computed_twice(
    two_sessions: tuple[Factory, str],
) -> None:
    """Releasing the snapshot must not let two operators each add 30 days to the
    same live expiry and end up with one extension."""
    factory, _url = two_sessions
    account_id = await _live_account(factory)

    async def another_operator_starts_the_same_operation() -> None:
        async with factory() as other:
            other.add(
                ServiceOperation(
                    account_id=account_id,
                    kind="extend_days",
                    target_expire_unix=_in(35),
                    requested_amount=30,
                    state="pending",
                    operation_key="other-admin",
                )
            )
            await other.commit()

    panel = _SlowReadPanel(another_operator_starts_the_same_operation, expire=_in(5), data_limit=GB)
    async with factory() as session:
        with pytest.raises(ServiceOpError):
            await extend_days(
                session, panel, account_id=account_id, days=30, operation_key="me"  # type: ignore[arg-type]
            )
        await session.rollback()

    assert panel.extend_calls == 0
    async with factory() as check:
        assert len((await check.scalars(select(ServiceOperation))).all()) == 1


@pytest.mark.asyncio
async def test_a_job_committing_during_a_link_rotation_does_not_lose_the_new_link(
    two_sessions: tuple[Factory, str],
) -> None:
    """The panel has rotated: the old link is dead, so the new one must be saved."""
    factory, _url = two_sessions
    account_id = await _live_account(factory)

    class Panel:
        @staticmethod
        def subscription_url_from_response(user: Any) -> str:
            return str(user.subscription_url)

        async def revoke_user_subscription(self, username: str) -> Any:
            await _scheduler_commits(factory)
            return SimpleNamespace(subscription_url=f"https://panel.example.com/sub/{username}/new")

    async with factory() as session:
        url = await revoke_link(session, Panel(), account_id=account_id)  # type: ignore[arg-type]
        await session.commit()

    assert url is not None and url.endswith("/new")
    async with factory() as check:
        assert await check.scalar(select(PasarGuardAccount.subscription_url)) == url


@pytest.mark.asyncio
async def test_a_job_committing_during_the_node_read_does_not_lose_the_watch_pass(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _url = two_sessions
    await _live_account(factory)

    class SlowPanel(FakeClient):
        async def list_nodes_simple(self) -> list[Any]:
            await _scheduler_commits(factory)
            return await super().list_nodes_simple()

    client = SlowPanel()
    client.nodes = {1: ("node-1", "connected", None)}
    for _pass in range(2):  # the second pass has rows to read before the panel call
        async with factory() as session:
            await check_panel_and_nodes(session, client)  # type: ignore[arg-type]
            await session.commit()

    async with factory() as check:
        rows = (await check.scalars(select(PanelNodeState))).all()
    assert {int(r.node_id): int(r.checks_in_status) for r in rows} == {0: 2, 1: 2}
