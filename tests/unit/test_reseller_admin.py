"""The bot's reseller admin rules after they moved to ``services/reseller_admin.py``.

Three behaviours changed in that move; each has a test here at the bot's own handler (the
code the owner's taps and typing reach), not only at the service:

* a typed number the database cannot hold, or a digit such as «²» that ``str.isdigit()``
  accepts and ``int()`` refuses, is the «فقط یک عدد بفرست» line, not an exception;
* a trial quota past the 32-bit column is that line too;
* a percent cut of a flat list is written under the economy tier even when the admin's
  screen state still carries the premium tier of the group they came from. Nothing reset
  that state on a change of group, and the bot used to write the cut under it, where the
  reseller's price is never read and the undo cannot find it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.handlers import admin as bot_admin
from bot.keyboards.admin import AdminCB
from bot.texts import texts
from services.reseller import (
    get_plan_price,
    parse_percent_input,
    set_plan_price,
)
from services.reseller_admin import (
    QUOTA_MAX,
    Cut,
    apply_cuts,
    parse_reseller_amount,
    parse_reseller_input,
)
from services.store_admin import get_catalog_view
from services.store_pricing import DEFAULT_TIER, PREMIUM_TIER
from tests.unit.web_reseller_helpers import (
    add_shop,
    bot_callback,
    bot_cut,
    bot_fsm,
    bot_open,
    bot_rate,
    bot_set_price,
    bot_settings,
    bot_type,
    bot_undo,
    fake_bot_screens,
    said,
    seed_catalog,
    snapshot,
)

Factory = tuple[async_sessionmaker[AsyncSession], str]
T = DEFAULT_TIER
LINE_INVALID = texts.admin_store_number_invalid.format(example="1200")
NINES = "9" * 30
UNREADABLE = ["²", "٣²", "⁵", NINES, str(2**63), "9" * 20, "1²3"]


@pytest.fixture(autouse=True)
def _bot_screens_are_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bot_screens(monkeypatch)


@pytest_asyncio.fixture
async def shop(two_sessions: Factory) -> AsyncIterator[SimpleNamespace]:
    factory, _ = two_sessions
    cat = await seed_catalog(factory)
    uid = await add_shop(factory)
    yield SimpleNamespace(factory=factory, cat=cat, uid=uid)


@pytest.mark.parametrize("text", UNREADABLE)
def test_a_number_the_database_cannot_hold_does_not_parse(text: str) -> None:
    assert parse_reseller_amount(text) is None
    for field in ("plan", "gb", "day", "quota", "tprice"):
        assert parse_reseller_input(field, text) is None


@pytest.mark.parametrize(
    ("text", "value"),
    [("150000", 150_000), ("150,000", 150_000), ("۱۵۰٬۰۰۰", 150_000), (str(2**63 - 1), 2**63 - 1)],
)
def test_the_numbers_the_bot_always_read_still_parse(text: str, value: int) -> None:
    assert parse_reseller_input("plan", text) == value


def test_zero_is_a_rate_but_not_a_typo() -> None:
    assert parse_reseller_input("gb", "0") == 0 and parse_reseller_input("gb", "۰") == 0
    assert parse_reseller_input("gb", "00") is None, "only a lone zero"
    assert parse_reseller_input("quota", str(QUOTA_MAX)) == QUOTA_MAX
    assert parse_reseller_input("quota", str(QUOTA_MAX + 1)) is None
    assert (
        parse_reseller_input("gb", str(QUOTA_MAX + 1)) == QUOTA_MAX + 1
    ), "only the quota is 32-bit"


@pytest.mark.parametrize("text", UNREADABLE)
@pytest.mark.asyncio
async def test_a_plan_price_the_database_cannot_hold_is_the_invalid_line(shop, text: str) -> None:
    c = shop.cat
    before = await snapshot(shop.factory, shop.uid)
    async with shop.factory() as s:
        reply = await bot_set_price(s, shop.uid, c.tabbed, c.a50, 30, T, text)
    assert said(reply) == LINE_INVALID
    assert await snapshot(shop.factory, shop.uid) == before


@pytest.mark.parametrize("field", ["gb", "day", "quota", "tprice"])
@pytest.mark.parametrize("text", UNREADABLE)
@pytest.mark.asyncio
async def test_a_rate_the_database_cannot_hold_is_the_invalid_line(shop, field, text) -> None:
    before = await snapshot(shop.factory, shop.uid)
    async with shop.factory() as s:
        reply = await bot_rate(s, shop.uid, field, text)
    assert said(reply) == LINE_INVALID
    assert await snapshot(shop.factory, shop.uid) == before


@pytest.mark.asyncio
async def test_a_trial_quota_past_the_32_bit_column_is_the_invalid_line(shop) -> None:
    async with shop.factory() as s:
        assert said(await bot_rate(s, shop.uid, "quota", str(2**31))) == LINE_INVALID
        assert said(await bot_rate(s, shop.uid, "quota", "5000000000")) == LINE_INVALID
    assert (await snapshot(shop.factory, shop.uid))["rate"] is None
    async with shop.factory() as s:
        await bot_rate(s, shop.uid, "quota", str(2**31 - 1))
    assert (await snapshot(shop.factory, shop.uid))["rate"] == (0, 0, 2**31 - 1, 0)


@pytest.mark.parametrize("text", ["²", "⁵", "1²", "٪²"])
@pytest.mark.asyncio
async def test_a_percent_like_a_superscript_digit_is_the_percent_line(shop, text: str) -> None:
    assert parse_percent_input(text) is None
    c = shop.cat
    async with shop.factory() as s:
        await set_plan_price(s, shop.uid, c.a50, 140_000, duration_days=30, tier=T)
        await s.commit()
    before = await snapshot(shop.factory, shop.uid)
    async with shop.factory() as s:
        reply = await bot_cut(s, shop.uid, c.tabbed, 30, T, text)
    assert said(reply) == texts.admin_reseller_pct_invalid
    assert await snapshot(shop.factory, shop.uid) == before


@pytest.mark.asyncio
async def test_a_plan_price_of_zero_is_still_the_zero_line_not_the_number_line(shop) -> None:
    c = shop.cat
    async with shop.factory() as s:
        reply = await bot_set_price(s, shop.uid, c.tabbed, c.a50, 30, T, "0")
    assert said(reply) == "قیمت باید بزرگ‌تر از صفر باشد."


# --------------------------------------------------------------------------- #
# A flat list's cut is written under the economy tier
# --------------------------------------------------------------------------- #


async def _from_the_premium_tab_to_the_flat_group(
    s: AsyncSession, uid: int, c: SimpleNamespace
):  # type: ignore[no-untyped-def]
    """The owner opens the tabbed group, taps its premium tab, then taps the flat group."""
    settings = bot_settings()
    state = bot_fsm()
    await bot_open(s, state, uid, c.tabbed, 30, T)
    await bot_admin.admin_reseller_matrix_tier(
        bot_callback(f"{AdminCB.RS_TIER}{PREMIUM_TIER}"), state, s, settings, None
    )
    view = await get_catalog_view(s)
    flat_id = next(cat.id for cat in view.categories if str(cat.slug) == c.flat)
    await bot_admin.admin_reseller_matrix_cat(
        bot_callback(f"{AdminCB.RS_CAT}{flat_id}"), state, s, settings, None
    )
    return state


@pytest.mark.asyncio
async def test_the_premium_tier_of_another_group_survives_the_switch_to_a_flat_group(shop) -> None:
    """Why the next test matters: nothing in the bot's flow resets the tier on a group change."""
    async with shop.factory() as s:
        state = await _from_the_premium_tab_to_the_flat_group(s, shop.uid, shop.cat)
        data = await state.get_data()
    assert data["rs_flat"] is True, "the screen is the flat group's"
    assert data["rs_sel_tier"] == PREMIUM_TIER, "and the premium tier is still in the state"


@pytest.mark.asyncio
async def test_a_flat_cut_is_written_under_economy_when_the_screen_state_says_premium(shop) -> None:
    c, uid = shop.cat, shop.uid
    async with shop.factory() as s:
        await set_plan_price(s, uid, c.f10, 55_000, duration_days=30, tier=T)
        await set_plan_price(s, uid, c.f50, 240_000, duration_days=60, tier=T)
        await s.commit()
    async with shop.factory() as s:
        state = await _from_the_premium_tab_to_the_flat_group(s, uid, c)
        await bot_admin.admin_reseller_pct_ask(
            bot_callback(AdminCB.RS_PCT), state, bot_settings(), None
        )
        await bot_type(s, state, "10")
        await bot_admin.admin_reseller_pct_confirm(
            bot_callback(AdminCB.RS_PCT_OK), state, s, bot_settings(), None
        )
        assert (await state.get_data())["rs_sel_tier"] == PREMIUM_TIER, "still the stale tier"
    rows = (await snapshot(shop.factory, uid))["prices"]
    assert rows == sorted(
        [(c.f10, 30, T, 49_000), (c.f50, 60, T, 216_000)]
    ), "the economy cells moved and no premium cell was created"
    async with shop.factory() as s:
        assert await get_plan_price(s, uid, c.f10, duration_days=30, tier=PREMIUM_TIER) is None
    # the undo (which looks under economy) finds the cut and restores it
    async with shop.factory() as s:
        callback = await bot_undo(s, uid, c.flat, 30, PREMIUM_TIER)
        callback.answer.assert_awaited()
    assert (await snapshot(shop.factory, uid))["prices"] == sorted(
        [(c.f10, 30, T, 55_000), (c.f50, 60, T, 240_000)]
    )


@pytest.mark.asyncio
async def test_apply_cuts_writes_a_flat_list_under_economy_and_a_tab_under_its_own_tier(
    shop,
) -> None:
    c, uid = shop.cat, shop.uid
    async with shop.factory() as s:
        await set_plan_price(s, uid, c.f10, 55_000, duration_days=30, tier=T)
        await set_plan_price(s, uid, c.a50, 190_000, duration_days=30, tier=PREMIUM_TIER)
        await s.commit()
    async with shop.factory() as s:
        assert (
            await apply_cuts(
                s, uid, flat=True, tier=PREMIUM_TIER, pct=10, cuts=[Cut(30, c.f10, 55_000, 49_000)]
            )
            == 1
        )
        assert (
            await apply_cuts(
                s,
                uid,
                flat=False,
                tier=PREMIUM_TIER,
                pct=10,
                cuts=[Cut(30, c.a50, 190_000, 171_000)],
            )
            == 1
        )
        await s.commit()
    assert (await snapshot(shop.factory, uid))["prices"] == sorted(
        [(c.f10, 30, T, 49_000), (c.a50, 30, PREMIUM_TIER, 171_000)]
    )
