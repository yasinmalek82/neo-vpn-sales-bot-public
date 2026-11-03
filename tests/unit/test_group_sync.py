"""Changing a store group's panel group must be able to move its live services.

A panel user gets its groups once, at create time, so a group re-pointed later
left every existing service with an empty link. These tests pin which accounts
count as «in» a store group, that a move reads first and writes the absolute
list, that one bad account never stops the rest, and that the owner is asked
only when there is something to ask about.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserModify, UserResponse, UserStatus
from bot.config import Settings
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.group_sync import (
    GroupSyncResult,
    accounts_of_category,
    move_accounts_to_groups,
    target_groups_for_category,
)
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    update_draft_category,
)

OWNER_ID = 4242
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    DEFAULT_PANEL_GROUP_IDS=[1],
    ADMIN_TELEGRAM_IDS=[OWNER_ID],
    STORE_OWNER_TELEGRAM_IDS=[OWNER_ID],
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class _World:
    """Catalog + users + orders. Names map to the panel usernames created."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.plan_id = 0
        self.user_id = 0
        self.n = 0

    async def setup(self) -> None:
        user = User(
            telegram_id=500,
            username="u",
            first_name="U",
            language_code="fa",
            referral_code="R0000500",
            is_admin=False,
        )
        self.session.add(user)
        plan = Plan(
            name="p",
            description="p",
            data_limit_gb=5,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        self.session.add(plan)
        await self.session.flush()
        self.user_id, self.plan_id = user.id, plan.id

    async def service(
        self,
        username: str,
        *,
        package_slug: str | None = None,
        trial_slug: str | None = None,
        status: OrderStatus = OrderStatus.ACTIVE,
    ) -> None:
        self.n += 1
        self.session.add(
            Order(
                user_id=self.user_id,
                plan_id=self.plan_id,
                status=status,
                price_toman=0,
                discount_toman=0,
                force_new=True,
                panel_username=username,
                store_package_slug=package_slug,
                trial_category_slug=trial_slug,
            )
        )
        self.session.add(
            PasarGuardAccount(
                user_id=self.user_id,
                panel_user_id=1000 + self.n,
                panel_username=username,
            )
        )
        await self.session.flush()


async def _shelf(session: AsyncSession) -> dict[str, Any]:
    """«حجمی» (groups [11]) with an inheriting child, a self-grouped child, and a
    separate «نامحدود» group; a plan in each."""
    volume = await create_draft_category(session, title_fa="حجمی", kind="volume")
    other = await create_draft_category(session, title_fa="نامحدود", kind="unlimited")
    await update_draft_category(session, volume.id, panel_group_ids=[11])
    await update_draft_category(session, other.id, panel_group_ids=[77])
    inherit = await create_draft_category(
        session, title_fa="ارث‌بر", kind="volume", parent_id=volume.id
    )
    own = await create_draft_category(session, title_fa="جدا", kind="volume", parent_id=volume.id)
    await update_draft_category(session, own.id, panel_group_ids=[33])
    slugs: dict[str, Any] = {}
    slugs["volume_cat"] = str(volume.slug)  # a group with children holds no plans
    for key, cat in (("other", other), ("inherit", inherit), ("own", own)):
        pkg = await create_draft_package(
            session,
            category_id=cat.id,
            title_fa=f"پلن {key}",
            fixed_price_toman=100_000,
            **(
                {"volume_gb": 50, "duration_days": 30}
                if key != "other"
                else {"concurrent_users": 1, "duration_months": 1}
            ),
        )
        slugs[key] = str(pkg.slug)
        slugs[f"{key}_cat"] = str(cat.slug)
    await commit_and_activate(session, notes="seed")
    view = await get_catalog_view(session)
    slugs["ids"] = {c.slug: c.id for c in view.categories}
    return slugs


async def _world(session: AsyncSession) -> tuple[dict[str, Any], _World]:
    shelf = await _shelf(session)
    w = _World(session)
    await w.setup()
    await w.service("trial_volume", trial_slug=shelf["volume_cat"])
    await w.service("paid_inherit", package_slug=shelf["inherit"])
    await w.service("paid_own", package_slug=shelf["own"])
    await w.service("paid_other", package_slug=shelf["other"])
    await w.service("expired_volume", package_slug=shelf["inherit"], status=OrderStatus.EXPIRED)
    return shelf, w


def _names(accounts: list[PasarGuardAccount]) -> set[str]:
    return {a.panel_username for a in accounts}


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_selection_covers_paid_trial_and_inheriting_child(session: AsyncSession) -> None:
    shelf, _ = await _world(session)
    got = await accounts_of_category(session, shelf["ids"][shelf["volume_cat"]])
    assert _names(got) == {"trial_volume", "paid_inherit"}


@pytest.mark.asyncio
async def test_selection_excludes_other_groups_self_grouped_child_and_dead_orders(
    session: AsyncSession,
) -> None:
    shelf, _ = await _world(session)
    got = _names(await accounts_of_category(session, shelf["ids"][shelf["volume_cat"]]))
    assert "paid_other" not in got  # another store group
    assert "paid_own" not in got  # child with its own panel group
    assert "expired_volume" not in got  # not ACTIVE
    other = _names(await accounts_of_category(session, shelf["ids"][shelf["other_cat"]]))
    assert other == {"paid_other"}


@pytest.mark.asyncio
async def test_a_service_switched_away_belongs_to_its_latest_order(
    session: AsyncSession,
) -> None:
    """After «پلن دیگه روی همین سرویس» the old order stays ACTIVE. Counting it
    would pull the customer back onto the product they paid to leave."""
    shelf, w = await _world(session)
    w.session.add(
        Order(
            user_id=w.user_id,
            plan_id=w.plan_id,
            status=OrderStatus.ACTIVE,
            price_toman=0,
            discount_toman=0,
            panel_username="paid_inherit",
            store_package_slug=shelf["other"],
            renew_resets=True,
        )
    )
    await w.session.flush()
    volume = await accounts_of_category(session, shelf["ids"][shelf["volume_cat"]])
    other = await accounts_of_category(session, shelf["ids"][shelf["other_cat"]])
    assert "paid_inherit" not in _names(volume)
    assert "paid_inherit" in _names(other)


@pytest.mark.asyncio
async def test_selection_finds_a_group_by_a_stale_id(session: AsyncSession) -> None:
    shelf, _ = await _world(session)
    old_id = shelf["ids"][shelf["volume_cat"]]
    await update_draft_category(session, old_id, title_fa="حجمی ۲")
    await commit_and_activate(session, notes="rename")  # mints new row ids
    got = await accounts_of_category(session, old_id)
    assert "paid_inherit" in _names(got)


@pytest.mark.asyncio
async def test_target_groups_follow_the_category_and_its_parent(session: AsyncSession) -> None:
    shelf, _ = await _world(session)
    ids = shelf["ids"]
    assert await target_groups_for_category(session, SETTINGS, ids[shelf["volume_cat"]]) == [11]
    assert await target_groups_for_category(session, SETTINGS, ids[shelf["inherit_cat"]]) == [11]
    assert await target_groups_for_category(session, SETTINGS, ids[shelf["own_cat"]]) == [33]


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #


class _Panel:
    def __init__(self, groups: dict[str, list[int]], broken: set[str] | None = None) -> None:
        self.groups = groups
        self.broken = broken or set()
        self.modified: list[tuple[str, UserModify]] = []
        self.client = AsyncMock()
        self.client.get_user = AsyncMock(side_effect=self._get)
        self.client.modify_user = AsyncMock(side_effect=self._modify)

    async def _get(self, username: str) -> UserResponse:
        if username in self.broken:
            raise RuntimeError("panel timeout")
        return UserResponse(
            id=1,
            username=username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            group_ids=self.groups[username],
        )

    async def _modify(self, username: str, payload: UserModify) -> None:
        self.modified.append((username, payload))


async def _accounts(session: AsyncSession, names: list[str]) -> list[PasarGuardAccount]:
    w = _World(session)
    await w.setup()
    for name in names:
        await w.service(name)
    return [
        PasarGuardAccount(user_id=w.user_id, panel_user_id=i, panel_username=n)
        for i, n in enumerate(names)
    ]


@pytest.mark.asyncio
async def test_move_counts_moved_already_and_failed_and_continues(session: AsyncSession) -> None:
    accounts = await _accounts(session, ["a", "boom", "b", "c"])
    panel = _Panel({"a": [1], "b": [22], "c": [22, 23]}, broken={"boom"})
    result = await move_accounts_to_groups(panel.client, accounts, [22, 23], from_groups=[1])
    # a is on the old group → moved; boom fails and the rest carry on;
    # b is on neither old nor new (a premium tier, a switched service) → left;
    # c is already on the new set.
    assert result == GroupSyncResult(moved=1, already=1, failed=["boom"], skipped=1)
    assert [u for u, _ in panel.modified] == ["a"]
    panel2 = _Panel({"a": [1], "b": [23, 22], "c": [22, 23]})
    result2 = await move_accounts_to_groups(
        panel2.client, accounts[:1] + accounts[2:], [22, 23], from_groups=[1]
    )
    assert result2 == GroupSyncResult(moved=1, already=2, failed=[])
    assert [u for u, _ in panel2.modified] == ["a"]


@pytest.mark.asyncio
async def test_a_service_on_another_group_is_never_moved(session: AsyncSession) -> None:
    """Premium tier or already switched: the panel shows a third group. Leave it."""
    accounts = await _accounts(session, ["premium"])
    panel = _Panel({"premium": [55]})
    result = await move_accounts_to_groups(panel.client, accounts, [22], from_groups=[11])
    assert result == GroupSyncResult(moved=0, already=0, failed=[], skipped=1)
    assert panel.modified == []


@pytest.mark.asyncio
async def test_move_writes_exactly_the_new_groups_and_nothing_else(session: AsyncSession) -> None:
    accounts = await _accounts(session, ["a"])
    panel = _Panel({"a": [1]})
    await move_accounts_to_groups(panel.client, accounts, [22], from_groups=[1])
    ((username, payload),) = panel.modified
    assert username == "a"
    assert payload.model_dump(exclude_none=True) == {"group_ids": [22]}


@pytest.mark.asyncio
async def test_move_to_no_groups_is_refused(session: AsyncSession) -> None:
    accounts = await _accounts(session, ["a"])
    panel = _Panel({"a": [1]})
    with pytest.raises(ValueError):
        await move_accounts_to_groups(panel.client, accounts, [], from_groups=[1])
    assert panel.modified == []


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


async def _type_group(session: AsyncSession, cat_id: int, text: str) -> SimpleNamespace:
    state = _state()
    await state.set_state(AdminStates.store_waiting_cat_groups)
    await state.update_data(store_cat_id=cat_id)
    msg = _typed(text)
    await ha.store_group_pick_typed(msg, state, SETTINGS, None, session)  # type: ignore[arg-type]
    return msg


def _cbs(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_owner_is_asked_when_groups_changed_and_services_exist(
    session: AsyncSession,
) -> None:
    shelf, _ = await _world(session)
    cat_id = shelf["ids"][shelf["volume_cat"]]
    msg = await _type_group(session, cat_id, "12")
    call = msg.answer.await_args
    assert "2 سرویس" in call.args[0]
    cbs = _cbs(call.kwargs["reply_markup"])
    moves = [c for c in cbs if c.startswith(StoreCB.CAT_GROUP_MOVE)]
    assert moves and moves[0].endswith(":11"), "the old group rides in the button"
    assert any(c.startswith(StoreCB.CAT_GROUP_KEEP) for c in cbs)


@pytest.mark.asyncio
async def test_no_question_when_groups_unchanged_or_nobody_is_in_the_group(
    session: AsyncSession,
) -> None:
    shelf, _ = await _world(session)
    same = await _type_group(session, shelf["ids"][shelf["volume_cat"]], "11")
    same.answer.assert_not_awaited()  # today's flow: the group screen, edited in place
    same.edit_text.assert_awaited()
    # «نامحدود» has one service; empty the shelf by choosing a group with none.
    lonely = await create_draft_category(session, title_fa="خالی", kind="volume")
    await commit_and_activate(session, notes="x")
    view = await get_catalog_view(session)
    lid = next(c.id for c in view.categories if c.slug == lonely.slug)
    msg = await _type_group(session, lid, "55")
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_yes_moves_every_account_through_the_panel(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf, _ = await _world(session)
    cat_id = shelf["ids"][shelf["volume_cat"]]
    await _type_group(session, cat_id, "12")
    panel = _Panel(
        {"trial_volume": [12], "paid_inherit": [11]},
    )
    panel.client.aclose = AsyncMock()
    monkeypatch.setattr(ha, "get_pasarguard_client", AsyncMock(return_value=panel.client))
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    cb = SimpleNamespace(
        data=f"{StoreCB.CAT_GROUP_MOVE}{cat_id}:11",
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        message=screen,
    )
    await ha.store_cat_group_move(cb, SETTINGS, None, session)  # type: ignore[arg-type]
    assert sorted(u for u, _ in panel.modified) == ["paid_inherit"]
    assert all(p.group_ids == [12] for _, p in panel.modified)
    panel.client.aclose.assert_awaited_once()
    text = screen.edit_text.await_args.args[0]
    assert "منتقل‌شده: 1" in text and "از قبل درست بودن: 1" in text


@pytest.mark.asyncio
async def test_no_leaves_the_panel_alone(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf, _ = await _world(session)
    cat_id = shelf["ids"][shelf["volume_cat"]]
    opener = AsyncMock()
    monkeypatch.setattr(ha, "get_pasarguard_client", opener)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    cb = SimpleNamespace(
        data=f"{StoreCB.CAT_GROUP_KEEP}{cat_id}",
        from_user=SimpleNamespace(id=OWNER_ID),
        answer=AsyncMock(),
        message=screen,
    )
    await ha.store_cat_group_keep(cb, SETTINGS, None)  # type: ignore[arg-type]
    opener.assert_not_called()
    assert screen.edit_text.await_args.kwargs["reply_markup"] is not None
