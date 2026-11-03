"""``status_map_for_services``: the one loop behind the bot's lists and the web pages.

The bot's «سرویس‌های من» and the Mini App's personal pages both ask it, so a
service reads the same on both. Panel failures never reach the caller.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from bot.config import Settings
from bot.handlers import account as bot_account
from database.models import PasarGuardAccount
from services.accounts import status_map_for_services
from tests.unit.web_personal_helpers import (
    Factory,
    FakePanel,
    make_person,
    make_service,
    panel_user,
)

Pair = tuple[Factory, str]


async def _accounts(factory: Factory) -> list[PasarGuardAccount]:
    async with factory() as session:
        return list((await session.execute(select(PasarGuardAccount))).scalars().all())


@pytest.mark.asyncio
async def test_a_live_row_carries_the_panels_status_expiry_and_usage(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    uid = await make_person(factory, 9001)
    await make_service(factory, uid, username="a_u1", label="الف", expires_in_days=30)
    (account,) = await _accounts(factory)
    panel = FakePanel({"a_u1": panel_user("a_u1", used_gb=2.0, limit_gb=10, expires_in_days=5)})
    async with factory() as session:
        result = await status_map_for_services(session, panel, [account])  # type: ignore[arg-type]
    live = result[account.id]
    assert live.from_panel and live.status == "active"
    assert (live.used_traffic, live.data_limit) == (2 * 1024**3, 10 * 1024**3)
    assert live.expire is not None and (live.expire - panel.users["a_u1"].expire).total_seconds() == 0  # type: ignore[union-attr,operator]


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_orders_expiry_and_nothing_else(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    uid = await make_person(factory, 9001)
    await make_service(factory, uid, username="a_u1", label="الف", expires_in_days=30)
    (account,) = await _accounts(factory)
    async with factory() as session:
        result = await status_map_for_services(
            session, FakePanel({"a_u1": RuntimeError("boom")}), [account]  # type: ignore[arg-type]
        )
    live = result[account.id]
    assert not live.from_panel
    assert (live.status, live.used_traffic, live.data_limit) == (None, None, None)
    assert live.expire is not None


@pytest.mark.asyncio
async def test_a_slow_read_and_a_spent_budget_skip_the_panel(two_sessions: Pair) -> None:
    factory, _ = two_sessions
    uid = await make_person(factory, 9001)
    for name in ("a_u1", "b_u2", "c_u3"):
        await make_service(factory, uid, username=name, label=name)

    class Slow(FakePanel):
        async def get_user(self, username: str):  # type: ignore[no-untyped-def]
            self.reads.append(username)
            await asyncio.sleep(5)
            return panel_user(username)

    panel = Slow({})
    accounts = await _accounts(factory)
    async with factory() as session:
        result = await status_map_for_services(
            session, panel, accounts, read_timeout=0.05, budget=0.08  # type: ignore[arg-type]
        )
    assert len(result) == 3 and not any(r.from_panel for r in result.values())
    assert len(panel.reads) < 3, "after the budget the remaining rows never ask the panel"


@pytest.mark.asyncio
async def test_the_bots_list_reads_the_same_function(
    two_sessions: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    uid = await make_person(factory, 9001)
    await make_service(factory, uid, username="a_u1", label="الف")
    await make_service(factory, uid, username="b_u2", label="ب")
    accounts = await _accounts(factory)
    panel = FakePanel({"a_u1": panel_user("a_u1", expires_in_days=7), "b_u2": RuntimeError("x")})

    async def fake_client(*_a: object, **_kw: object) -> FakePanel:
        return panel

    monkeypatch.setattr(bot_account, "get_pasarguard_client", fake_client)
    async with factory() as session:
        got = await bot_account._status_map_for_services(session, Settings(BOT_TOKEN="t"), accounts)
        direct = await status_map_for_services(session, panel, accounts)  # type: ignore[arg-type]
    assert panel.closed
    assert got == {i: (item.status, item.expire) for i, item in direct.items()}
    by_name = {a.panel_username: a.id for a in accounts}
    assert got[by_name["a_u1"]][0] == "active" and got[by_name["b_u2"]][0] is None
