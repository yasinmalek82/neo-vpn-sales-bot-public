"""A free trial belongs to a store group: one per person per group.

The owner sells two products that need testing separately — «نامحدود اقتصادی»
on direct inbounds and «حجمی ویژه» on the tunnel — so a customer whose direct
trial failed on their ISP can still test the tunnel before paying. Each trial
runs on its own group's panel groups, or it would not test the product.

A trial taken before groups had trials was a direct one and counts against the
unlimited groups only. The admin reset voids trials instead of only clearing
``trial_claimed_at``, which once looked like a reset and did nothing.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserCreate, UserResponse, UserStatus
from api_client.exceptions import PasarGuardNotFoundError
from bot.config import Settings
from database.base import Base
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.plan import DataLimitResetStrategy
from services.customer_lookup import ManualAdjustError, reset_trial
from services.store_admin import (
    StoreAdminError,
    commit_and_activate,
    create_draft_category,
    get_catalog_view,
    set_category_trial,
    update_draft_category,
)
from services.store_catalog import category_reseller_trial, category_trial_shape
from services.trial import (
    TrialError,
    TrialOffer,
    issue_free_trial,
    issue_reseller_trial,
    list_trial_offers,
    trial_open_for,
    user_trial_eligible,
)

SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    BOT_TOKEN="test-token",
    DEFAULT_PANEL_GROUP_IDS=[1],
    ADMIN_TELEGRAM_IDS=[],
)
DIRECT_GROUPS = [21]
TUNNEL_GROUPS = [22]


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _catalog(
    session: AsyncSession,
    *,
    direct_trial: tuple[int, int] | None = (1, 1),
    tunnel_trial: tuple[int, int] | None = (1, 1),
    reseller_direct: bool = True,
) -> dict[str, str]:
    """The owner's catalog: direct unlimited and tunnel volume, own panel groups."""
    direct = await create_draft_category(session, title_fa="نامحدود اقتصادی", kind="unlimited")
    tunnel = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await update_draft_category(session, direct.id, panel_group_ids=DIRECT_GROUPS)
    await update_draft_category(session, tunnel.id, panel_group_ids=TUNNEL_GROUPS)
    if direct_trial:
        await set_category_trial(
            session, direct.id, data_limit_gb=direct_trial[0], duration_days=direct_trial[1]
        )
    if tunnel_trial:
        await set_category_trial(
            session, tunnel.id, data_limit_gb=tunnel_trial[0], duration_days=tunnel_trial[1]
        )
    if reseller_direct:
        await update_draft_category(session, direct.id, reseller_trial=True)
    await commit_and_activate(session, notes="seed")
    return {"direct": str(direct.slug), "tunnel": str(tunnel.slug)}


async def _user(session: AsyncSession, tg: int = 500, *, reseller: bool = False) -> User:
    user = User(
        telegram_id=tg,
        username=f"u{tg}",
        first_name="U",
        language_code="fa",
        referral_code=f"R{tg:07d}",
        is_admin=False,
        is_reseller=reseller,
    )
    session.add(user)
    await session.flush()
    return user


class _Panel:
    """A panel that remembers what it was asked to create."""

    def __init__(self) -> None:
        self.users: dict[str, UserResponse] = {}
        self.created: list[UserCreate] = []
        self.client = AsyncMock()
        self.client.get_user = AsyncMock(side_effect=self._get)
        self.client.create_user = AsyncMock(side_effect=self._create)
        self.client.subscription_url_from_response = lambda u: u.subscription_url

    async def _get(self, username: str) -> UserResponse:
        if username in self.users:
            return self.users[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    def _create(self, payload: UserCreate) -> UserResponse:
        self.created.append(payload)
        user = UserResponse(
            id=len(self.created),
            username=payload.username,
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=f"https://panel.example.com/sub/{payload.username}",
            data_limit=payload.data_limit,
        )
        self.users[payload.username] = user
        return user


async def _offer(session: AsyncSession, slug: str) -> TrialOffer:
    return next(o for o in await list_trial_offers(session, SETTINGS) if o.slug == slug)


async def _take(session: AsyncSession, panel: _Panel, user: User, offer: TrialOffer) -> Any:
    return await issue_free_trial(
        session,
        panel.client,
        user_id=user.id,
        category_slug=offer.slug,
        counts_legacy=offer.counts_legacy,
        data_limit_gb=offer.data_limit_gb,
        duration_days=offer.duration_days,
        hwid_limit=1,
        group_ids=offer.group_ids,
    )


async def _personal_trials(session: AsyncSession, user: User) -> list[Order]:
    return list(
        (
            await session.execute(
                select(Order)
                .where(Order.user_id == user.id, Order.trial_kind == "personal")
                .order_by(Order.id)
            )
        )
        .scalars()
        .all()
    )


async def _legacy_trial(session: AsyncSession, user: User, *, kind: str | None) -> None:
    """A trial order from before groups had trials, with its panel account."""
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
            user_id=user.id,
            plan_id=plan.id,
            status=OrderStatus.ACTIVE,
            price_toman=0,
            discount_toman=0,
            force_new=True,
            trial_kind=kind,
            panel_username="legacy_trial",
        )
    )
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=999,
            panel_username="legacy_trial",
            subscription_url="https://panel.example.com/sub/legacy_trial",
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Offers come from the live catalog
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_each_offer_runs_on_its_own_groups_panel_groups(session: AsyncSession) -> None:
    slugs = await _catalog(session, direct_trial=(1, 1), tunnel_trial=(2, 3))
    offers = {o.slug: o for o in await list_trial_offers(session, SETTINGS)}
    direct, tunnel = offers[slugs["direct"]], offers[slugs["tunnel"]]
    assert (direct.group_ids, direct.data_limit_gb, direct.duration_days) == (DIRECT_GROUPS, 1, 1)
    assert (tunnel.group_ids, tunnel.data_limit_gb, tunnel.duration_days) == (TUNNEL_GROUPS, 2, 3)
    # Only the direct (unlimited) product inherits a pre-group trial.
    assert direct.counts_legacy is True
    assert tunnel.counts_legacy is False


@pytest.mark.asyncio
async def test_a_group_without_a_trial_offers_none(session: AsyncSession) -> None:
    slugs = await _catalog(session, tunnel_trial=None)
    assert [o.slug for o in await list_trial_offers(session, SETTINGS)] == [slugs["direct"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_by", ["disabled", "no_new_sale"])
async def test_a_closed_group_offers_no_trial(session: AsyncSession, closed_by: str) -> None:
    slugs = await _catalog(session)
    view = await get_catalog_view(session)
    tunnel = next(c for c in view.categories if c.slug == slugs["tunnel"])
    if closed_by == "disabled":
        await update_draft_category(session, tunnel.id, enabled=False)
    else:
        await update_draft_category(session, tunnel.id, allow_new_sale=False)
    await commit_and_activate(session, notes="close")
    assert [o.slug for o in await list_trial_offers(session, SETTINGS)] == [slugs["direct"]]


@pytest.mark.asyncio
async def test_a_child_of_a_closed_parent_offers_no_trial(session: AsyncSession) -> None:
    """The purchase menu cannot reach it, so neither may the trial."""
    parent = await create_draft_category(session, title_fa="حجمی", kind="volume")
    await update_draft_category(session, parent.id, panel_group_ids="7")
    child = await create_draft_category(
        session, title_fa="ویژه", kind="volume", parent_id=parent.id
    )
    await set_category_trial(session, child.id, data_limit_gb=1, duration_days=1)
    await commit_and_activate(session, notes="seed")
    assert [o.slug for o in await list_trial_offers(session, SETTINGS)] == [str(child.slug)]

    view = await get_catalog_view(session)
    live_parent = next(c for c in view.categories if c.slug == parent.slug)
    await update_draft_category(session, live_parent.id, enabled=False)
    await commit_and_activate(session, notes="close parent")
    assert await list_trial_offers(session, SETTINGS) == []


@pytest.mark.asyncio
async def test_reseller_offers_follow_the_reseller_switch(session: AsyncSession) -> None:
    slugs = await _catalog(session, reseller_direct=True)
    offers = await list_trial_offers(session, SETTINGS, reseller=True)
    assert [o.slug for o in offers] == [slugs["direct"]]


# --------------------------------------------------------------------------- #
# One per person per group
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_then_tunnel_are_two_trials_on_their_own_groups(
    session: AsyncSession,
) -> None:
    """The funnel the owner designed: direct fails on my ISP → test the tunnel."""
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()

    direct = await _take(session, panel, user, await _offer(session, slugs["direct"]))
    tunnel = await _take(session, panel, user, await _offer(session, slugs["tunnel"]))

    assert direct.already_claimed is False and tunnel.already_claimed is False
    assert direct.order.id != tunnel.order.id
    assert [p.group_ids for p in panel.created] == [DIRECT_GROUPS, TUNNEL_GROUPS]
    assert [o.trial_category_slug for o in await _personal_trials(session, user)] == [
        slugs["direct"],
        slugs["tunnel"],
    ]


@pytest.mark.asyncio
async def test_a_second_tap_on_the_same_group_resumes_and_creates_nothing(
    session: AsyncSession,
) -> None:
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    offer = await _offer(session, slugs["tunnel"])

    first = await _take(session, panel, user, offer)
    again = await _take(session, panel, user, offer)

    assert again.already_claimed is True
    assert again.order.id == first.order.id
    assert len(panel.created) == 1


@pytest.mark.asyncio
async def test_eligibility_lasts_until_every_group_is_taken(session: AsyncSession) -> None:
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    assert await user_trial_eligible(session, SETTINGS, user) is True

    await _take(session, panel, user, await _offer(session, slugs["direct"]))
    assert await user_trial_eligible(session, SETTINGS, user) is True
    assert await trial_open_for(session, user, await _offer(session, slugs["direct"])) is False

    await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    assert await user_trial_eligible(session, SETTINGS, user) is False


@pytest.mark.asyncio
async def test_the_database_refuses_a_second_live_trial_in_one_group(
    session: AsyncSession,
) -> None:
    """The partial unique index, not only the code, keeps it to one."""
    user = await _user(session)
    plan = Plan(
        name="trial-1GB-1d",
        description="t",
        data_limit_gb=1,
        duration_days=1,
        price_toman=0,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    row = {
        "user_id": user.id,
        "plan_id": plan.id,
        "status": OrderStatus.ACTIVE.value,
        "price_toman": 0,
        "discount_toman": 0,
        "wallet_applied_toman": 0,
        "force_new": True,
        "trial_kind": "personal",
        "trial_category_slug": "unlimited",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    await session.execute(insert(Order.__table__).values(**row))
    # Another group: allowed.
    await session.execute(
        insert(Order.__table__).values(**{**row, "trial_category_slug": "volume"})
    )
    # A voided one in the same group: allowed.
    await session.execute(
        insert(Order.__table__).values(**{**row, "trial_voided_at": datetime.now(UTC)})
    )
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert(Order.__table__).values(**row))


# --------------------------------------------------------------------------- #
# Trials from before groups had trials
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_pre_group_trial_is_the_direct_one(session: AsyncSession) -> None:
    slugs = await _catalog(session)
    user = await _user(session)
    await _legacy_trial(session, user, kind="personal")
    user.trial_claimed_at = datetime.now(UTC)
    panel = _Panel()
    panel.users["legacy_trial"] = MagicMock()

    direct = await _take(session, panel, user, await _offer(session, slugs["direct"]))
    assert direct.already_claimed is True
    assert direct.order.panel_username == "legacy_trial"
    assert panel.created == []

    tunnel = await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    assert tunnel.already_claimed is False
    assert [p.group_ids for p in panel.created] == [TUNNEL_GROUPS]


@pytest.mark.asyncio
async def test_an_imported_claim_with_no_order_blocks_direct_only(session: AsyncSession) -> None:
    """What the old bot's import leaves: the flag, and no trial order."""
    slugs = await _catalog(session)
    user = await _user(session)
    user.trial_claimed_at = datetime.now(UTC)
    await session.flush()
    panel = _Panel()

    with pytest.raises(TrialError, match="trial_already_claimed"):
        await _take(session, panel, user, await _offer(session, slugs["direct"]))
    tunnel = await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    assert tunnel.already_claimed is False


@pytest.mark.asyncio
async def test_a_new_trial_elsewhere_does_not_read_as_an_imported_claim(
    session: AsyncSession,
) -> None:
    """``trial_claimed_at`` is also written by a new trial; it must not block direct."""
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    await session.refresh(user)
    assert user.trial_claimed_at is not None

    direct = await _take(session, panel, user, await _offer(session, slugs["direct"]))
    assert direct.already_claimed is False


@pytest.mark.asyncio
async def test_an_unknown_legacy_trial_blocks_direct_and_not_the_tunnel(
    session: AsyncSession,
) -> None:
    """A trial order of unknown kind is never guessed to be someone else's."""
    slugs = await _catalog(session)
    user = await _user(session)
    await _legacy_trial(session, user, kind=None)
    panel = _Panel()

    with pytest.raises(TrialError, match="trial_recovery_unsafe"):
        await _take(session, panel, user, await _offer(session, slugs["direct"]))
    tunnel = await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    assert tunnel.already_claimed is False


# --------------------------------------------------------------------------- #
# The admin reset actually resets
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reset_lets_them_take_the_trial_again(session: AsyncSession) -> None:
    """It used to clear the flag only; the next tap resumed the old trial."""
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    offer = await _offer(session, slugs["direct"])
    first = await _take(session, panel, user, offer)

    await reset_trial(session, user.id)
    assert await trial_open_for(session, user, offer) is True
    second = await _take(session, panel, user, offer)

    assert second.already_claimed is False
    assert second.order.id != first.order.id
    assert len(panel.created) == 2
    trials = await _personal_trials(session, user)
    assert trials[0].trial_voided_at is not None, "history stays, voided"
    assert trials[1].trial_voided_at is None


@pytest.mark.asyncio
async def test_reset_also_clears_an_imported_claim(session: AsyncSession) -> None:
    slugs = await _catalog(session)
    user = await _user(session)
    user.trial_claimed_at = datetime.now(UTC)
    await session.flush()
    await reset_trial(session, user.id)
    assert await trial_open_for(session, user, await _offer(session, slugs["direct"])) is True


@pytest.mark.asyncio
async def test_reset_refuses_someone_with_no_trial(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ManualAdjustError):
        await reset_trial(session, user.id)


# --------------------------------------------------------------------------- #
# Reseller giveaways
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_giveaway_runs_on_the_offer_group_and_records_it(session: AsyncSession) -> None:
    from services.reseller import set_rate

    slugs = await _catalog(session)
    shop = await _user(session, 900, reseller=True)
    await set_rate(session, shop.id, trial_quota_monthly=5)
    panel = _Panel()
    offer = (await list_trial_offers(session, SETTINGS, reseller=True))[0]
    for _ in range(2):
        await issue_reseller_trial(
            session,
            panel.client,
            user_id=shop.id,
            data_limit_gb=offer.data_limit_gb,
            duration_days=offer.duration_days,
            hwid_limit=1,
            group_ids=offer.group_ids,
            display_name="reza | 0912",
            service_name="reza",
            category_slug=offer.slug,
        )
    rows = (
        (await session.execute(select(Order).where(Order.trial_kind == "reseller"))).scalars().all()
    )
    # Many per shop, unlike a personal trial.
    assert [r.trial_category_slug for r in rows] == [slugs["direct"], slugs["direct"]]
    assert [p.group_ids for p in panel.created] == [DIRECT_GROUPS, DIRECT_GROUPS]


# --------------------------------------------------------------------------- #
# Store admin: the group's trial setting
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gb", "days"),
    [(101, 1), (-1, 1), (1, 0), (1, 31)],
)
async def test_set_category_trial_refuses_out_of_range(
    session: AsyncSession, gb: int, days: int
) -> None:
    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    with pytest.raises(StoreAdminError):
        await set_category_trial(session, cat.id, data_limit_gb=gb, duration_days=days)


@pytest.mark.asyncio
async def test_zero_switches_the_trial_off_and_keeps_the_days(session: AsyncSession) -> None:
    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await set_category_trial(session, cat.id, data_limit_gb=2, duration_days=3)
    assert category_trial_shape(cat) == (2, 3)
    await set_category_trial(session, cat.id, data_limit_gb=0, duration_days=0)
    assert category_trial_shape(cat) is None
    assert dict(cat.sales_flags)["trial_days"] == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 1", (1, 1)),
        ("۱ ۲", (1, 2)),
        ("2،3", (2, 3)),
        ("  0 ", (0, 0)),
        ("1", None),
        ("1 2 3", None),
        ("a b", None),
        ("", None),
    ],
)
def test_the_trial_prompt_parses_what_the_owner_types(
    raw: str, expected: tuple[int, int] | None
) -> None:
    from bot.handlers.store_admin import _parse_trial_input

    assert _parse_trial_input(raw) == expected


def _labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_the_reseller_switch_shows_only_with_a_trial(session: AsyncSession) -> None:
    """A switch on a group with no trial would save a flag nothing reads."""
    from bot.keyboards.store_admin import store_cat_trial_keyboard

    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    off = _labels(store_cat_trial_keyboard(cat))
    assert "🎟 تست رایگان: خاموش — تغییر" in off
    assert not any("تست نماینده" in label for label in off)

    await set_category_trial(session, cat.id, data_limit_gb=1, duration_days=2)
    on = _labels(store_cat_trial_keyboard(cat))
    assert "🎟 تست رایگان: ۱ گیگ • ۲ روز — تغییر" in on
    assert "❌ تست نماینده" in on


@pytest.mark.asyncio
async def test_the_owner_sets_a_trial_from_telegram_and_it_goes_live(
    session: AsyncSession,
) -> None:
    from bot.handlers import store_admin as handlers
    from bot.states import AdminStates

    owner_id = 4242
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS=[owner_id],
        STORE_OWNER_TELEGRAM_IDS=[owner_id],
    )
    cat = await create_draft_category(session, title_fa="حجمی ویژه", kind="volume")
    await update_draft_category(session, cat.id, panel_group_ids="7")
    await commit_and_activate(session, notes="seed")
    view = await get_catalog_view(session)
    live = next(c for c in view.categories if c.slug == cat.slug)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(AdminStates.store_waiting_cat_trial)
    await state.update_data(store_cat_id=int(live.id))
    message = SimpleNamespace(
        text="۱ ۱",
        from_user=SimpleNamespace(id=owner_id),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )
    admin = SimpleNamespace(id=1, telegram_id=owner_id, is_admin=True)
    await handlers.store_cat_trial_input(
        message, state, settings, admin, session  # type: ignore[arg-type]
    )

    assert await state.get_state() is None
    view = await get_catalog_view(session)
    published = next(c for c in view.categories if c.slug == cat.slug)
    assert category_trial_shape(published) == (1, 1)
    assert category_reseller_trial(published) is False
    offers = await list_trial_offers(session, SETTINGS)
    assert [o.slug for o in offers] == [str(cat.slug)]


# --------------------------------------------------------------------------- #
# The customer's screen
# --------------------------------------------------------------------------- #

_DIRECT = TrialOffer(
    slug="unlimited",
    title_fa="نامحدود اقتصادی",
    data_limit_gb=1,
    duration_days=1,
    group_ids=DIRECT_GROUPS,
    counts_legacy=True,
)
_TUNNEL = TrialOffer(
    slug="volume",
    title_fa="حجمی ویژه",
    data_limit_gb=1,
    duration_days=1,
    group_ids=TUNNEL_GROUPS,
    counts_legacy=False,
)


def _screen(
    monkeypatch: pytest.MonkeyPatch, offers: list[TrialOffer]
) -> tuple[SimpleNamespace, AsyncMock]:
    from bot.handlers import referral

    issue = AsyncMock(side_effect=TrialError("trial_already_claimed"))
    monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=offers))
    monkeypatch.setattr(referral, "get_trial_hwid_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(referral, "get_pasarguard_client", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(referral, "issue_free_trial", issue)
    return SimpleNamespace(answer=AsyncMock()), issue


async def _render(message: SimpleNamespace, *, slug: str | None = None) -> None:
    from bot.handlers.referral import render_trial

    await render_trial(
        message,  # type: ignore[arg-type]
        AsyncMock(),
        SimpleNamespace(trial_enabled=True),  # type: ignore[arg-type]
        SimpleNamespace(id=7, is_reseller=False),  # type: ignore[arg-type]
        AsyncMock(),
        slug=slug,
    )


@pytest.mark.asyncio
async def test_two_trials_ask_which_before_creating_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.keyboards.referral import ReferralCB

    message, issue = _screen(monkeypatch, [_DIRECT, _TUNNEL])
    await _render(message)

    issue.assert_not_awaited()
    body = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "یه بار" in body, "the rule the picker states is the rule the bot keeps"
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{ReferralCB.TRIAL_GROUP_PREFIX}unlimited" in callbacks
    assert f"{ReferralCB.TRIAL_GROUP_PREFIX}volume" in callbacks
    assert all(len(cb.encode()) <= 64 for cb in callbacks), "Telegram's callback_data cap"


@pytest.mark.asyncio
async def test_the_picked_group_is_the_one_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    message, issue = _screen(monkeypatch, [_DIRECT, _TUNNEL])
    await _render(message, slug="volume")

    kwargs = issue.await_args.kwargs
    assert kwargs["category_slug"] == "volume"
    assert kwargs["counts_legacy"] is False
    assert kwargs["group_ids"] == TUNNEL_GROUPS


@pytest.mark.asyncio
async def test_a_single_trial_is_issued_without_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    message, issue = _screen(monkeypatch, [_DIRECT])
    await _render(message)
    assert issue.await_args.kwargs["category_slug"] == "unlimited"


@pytest.mark.asyncio
async def test_a_stale_group_button_says_so_and_offers_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.texts import texts

    message, issue = _screen(monkeypatch, [_DIRECT])
    await _render(message, slug="gone")
    issue.assert_not_awaited()
    assert message.answer.await_args.args[0] == texts.trial_group_gone


@pytest.mark.asyncio
async def test_no_group_with_a_trial_reads_as_trial_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.texts import texts

    message, issue = _screen(monkeypatch, [])
    await _render(message)
    issue.assert_not_awaited()
    assert message.answer.await_args.args[0] == texts.trial_disabled


@pytest.mark.asyncio
async def test_a_reseller_picks_the_group_then_names_the_customer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picked group rides through the label step to the giveaway."""
    from bot.handlers import referral
    from services.reseller import set_rate

    shop = await _user(session, 901, reseller=True)
    await set_rate(session, shop.id, trial_quota_monthly=5)
    await session.commit()
    monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=[_DIRECT, _TUNNEL]))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    message = SimpleNamespace(answer=AsyncMock())

    await referral.render_trial(
        message,  # type: ignore[arg-type]
        session,
        SimpleNamespace(trial_enabled=True),  # type: ignore[arg-type]
        shop,
        state,
        slug="volume",
    )
    assert (await state.get_data())["trial_slug"] == "volume"

    issue = AsyncMock(side_effect=TrialError("trial_quota_exhausted"))
    monkeypatch.setattr(referral, "issue_reseller_trial", issue)
    monkeypatch.setattr(referral, "get_trial_hwid_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(referral, "get_pasarguard_client", AsyncMock(return_value=AsyncMock()))
    from services.customer_label import CustomerLabel

    await referral.continue_trial_with_customer_label(
        message,  # type: ignore[arg-type]
        state,
        session,
        SimpleNamespace(trial_enabled=True, wallet_provision_fail_credit_max_n=3),  # type: ignore[arg-type]
        shop,
        customer=CustomerLabel(name="reza", phone=None),
    )
    assert issue.await_args.kwargs["category_slug"] == "volume"
    assert issue.await_args.kwargs["group_ids"] == TUNNEL_GROUPS


@pytest.mark.asyncio
async def test_a_group_switched_off_mid_label_is_not_given_away(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot.handlers import referral
    from bot.texts import texts
    from services.customer_label import CustomerLabel

    shop = await _user(session, 902, reseller=True)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.update_data(trial_slug="volume")
    monkeypatch.setattr(referral, "list_trial_offers", AsyncMock(return_value=[_DIRECT]))
    issue = AsyncMock()
    monkeypatch.setattr(referral, "issue_reseller_trial", issue)
    monkeypatch.setattr(
        referral,
        "get_reseller_trial_quota",
        AsyncMock(return_value=SimpleNamespace(can_issue=True)),
    )
    message = SimpleNamespace(answer=AsyncMock())

    await referral.continue_trial_with_customer_label(
        message,  # type: ignore[arg-type]
        state,
        session,
        SimpleNamespace(trial_enabled=True),  # type: ignore[arg-type]
        shop,
        customer=CustomerLabel(name="reza", phone=None),
    )
    issue.assert_not_awaited()
    assert message.answer.await_args.args[0] == texts.trial_group_gone


@pytest.mark.asyncio
async def test_personal_trial_count_stays_one_per_group(session: AsyncSession) -> None:
    """Belt and braces over the handler: many taps, two groups, two orders."""
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    for _ in range(3):
        for key in ("direct", "tunnel"):
            await _take(session, panel, user, await _offer(session, slugs[key]))
    count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.trial_kind == "personal")
    )
    assert count == 2
    assert len(panel.created) == 2


@pytest.mark.asyncio
async def test_each_group_trial_reaches_the_report_group(session: AsyncSession) -> None:
    """Keyed on the telegram id, the second group's trial report was swallowed."""
    from database.models.report import ReportOutbox

    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    for key in ("direct", "tunnel"):
        await _take(session, panel, user, await _offer(session, slugs[key]))
    keys = (
        (
            await session.execute(
                select(ReportOutbox.event_key).where(ReportOutbox.event_key.like("trial:%"))
            )
        )
        .scalars()
        .all()
    )
    assert len(keys) == 2
    assert len(set(keys)) == 2


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("حجمی ویژه ⭐️", "تست حجمی ویژه"),
        ("نامحدود اقتصادی ♾️", "تست نامحدود اقتصادی"),
        (None, "تست رایگان"),
        ("⭐️", "تست رایگان"),
    ],
)
def test_a_trial_is_labelled_with_its_product(title: str | None, label: str) -> None:
    from services.accounts import trial_label

    assert trial_label(title) == label


@pytest.mark.asyncio
async def test_the_trial_row_names_the_group_it_tests(session: AsyncSession) -> None:
    """Two rows of «تست رایگان» could not be told apart; now each names its group."""
    slugs = await _catalog(session)
    user = await _user(session)
    panel = _Panel()
    direct = await _take(session, panel, user, await _offer(session, slugs["direct"]))
    tunnel = await _take(session, panel, user, await _offer(session, slugs["tunnel"]))
    assert direct.account.display_name == "تست نامحدود اقتصادی"
    assert tunnel.account.display_name == "تست حجمی ویژه"


@pytest.mark.asyncio
async def test_panel_settings_show_each_store_groups_own_panel_group(
    session: AsyncSession,
) -> None:
    """The store group is the only source: a group with none says it is not sold."""
    from bot.handlers.admin import _store_group_lines

    await _catalog(session)
    bare = await create_draft_category(session, title_fa="بدون گروه", kind="volume")
    await commit_and_activate(session, notes="seed bare")
    _ = bare
    text = await _store_group_lines(session, SETTINGS)
    lines = text.splitlines()
    direct = next(line for line in lines if "نامحدود اقتصادی" in line)
    tunnel = next(line for line in lines if "حجمی ویژه" in line)
    unset = next(line for line in lines if "بدون گروه" in line)
    assert "21" in direct and "⚠️" not in direct
    assert "22" in tunnel and "⚠️" not in tunnel
    assert "⚠️ تنظیم نشده — فروخته نمی‌شود" in unset
