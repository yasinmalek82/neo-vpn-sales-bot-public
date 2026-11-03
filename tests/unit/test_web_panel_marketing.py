"""«📣 پیام‌های بازاریابی» on the Mini App (step 4 of the panel plan): settings only.

What these defend, each with a test that goes red when the real line is broken:

* a web switch, percent or hours leaves the row the bot's real handler leaves;
* every campaign ships off; a GET, a missing field or a default value never switches
  anything on, and looking never creates the row;
* a campaign's percent stays editable while the campaign is off;
* these settings are not in the store version, so a form carries a token of its own: a
  stale form is refused, and a double-submitted toggle applies once and never flips back;
* every POST under ``/panel/marketing`` obeys one guard (login, admin, store owner, the
  token, GET writes nothing), and a route added later with no case here fails the build;
* the parsing and the refusal lines are the bot's, and nothing is written on a refusal.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp.test_utils import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import marketing_admin as ma
from bot.keyboards.marketing import CODE_OF_CAMPAIGN, MktCB
from bot.states import MarketingStates
from bot.texts import texts
from database.base import Base
from database.models.marketing import CAMPAIGNS, OFFER_CAMPAIGNS, MarketingSettings
from services.marketing_edit import TEXT_STALE_MARKETING, read_marketing
from services.marketing_settings import set_campaign, set_offer_percent
from tests.unit.web_panel_helpers import (
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    init_data,
    panel_client,
    panel_settings,
)

Factory = tuple[async_sessionmaker[AsyncSession], str]
OWNER = {INIT_HEADER: init_data(ENV_ADMIN)}
NINES = "9" * 30


def _owner_settings() -> Any:
    return panel_settings(STORE_OWNER_TELEGRAM_IDS=str(ENV_ADMIN))


@pytest_asyncio.fixture
async def fresh(two_sessions: Factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory, _ = two_sessions
    yield factory


async def _token(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as s:
        return (await read_marketing(s))[1]


async def _row(factory: async_sessionmaker[AsyncSession]) -> dict[str, object] | None:
    """The row with the clock-dependent fields as facts, so two databases can be compared."""
    async with factory() as s:
        row = await s.get(MarketingSettings, 1)
        if row is None:
            return None
        out: dict[str, object] = {}
        for name in CAMPAIGNS:
            out[f"{name}_on"] = getattr(row, f"{name}_enabled_at") is not None
        for name in OFFER_CAMPAIGNS:
            out[f"{name}_percent"] = getattr(row, f"{name}_offer_percent")
            out[f"{name}_hours"] = getattr(row, f"{name}_offer_hours")
        return out


async def _rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(MarketingSettings)) or 0)


async def _page(client: TestClient, headers: dict[str, str] = OWNER) -> str:
    resp = await client.get("/panel/marketing", headers=headers)
    assert resp.status == 200
    return await resp.text()


async def _post(
    client: TestClient,
    campaign: str,
    action: str,
    form: dict[str, str],
    *,
    seen: str | None,
    headers: dict[str, str] = OWNER,
) -> tuple[int, str]:
    data = dict(form)
    if seen is not None:
        data["seen"] = seen
    resp = await client.post(f"/panel/marketing/{campaign}/{action}", data=data, headers=headers)
    return resp.status, await resp.text()


def _card(html: str, campaign: str) -> str:
    start = html.index(f'id="camp-{campaign}"')
    nxt = html.find('id="camp-', start + 1)
    return html[start : nxt if nxt != -1 else len(html)]


# --------------------------------------------------------------------------- #
# The bot's own handlers, driven for parity
# --------------------------------------------------------------------------- #


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=ENV_ADMIN), answer=AsyncMock(), message=None
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=ENV_ADMIN),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ENV_ADMIN, user_id=ENV_ADMIN)
    )


BotDriver = Callable[[AsyncSession], Awaitable[None]]


def _bot_toggle(campaign: str) -> BotDriver:
    async def drive(s: AsyncSession) -> None:
        await ma.marketing_toggle(
            _callback(f"{MktCB.TOGGLE}{CODE_OF_CAMPAIGN[campaign]}"),
            _fsm(),
            s,
            _owner_settings(),
            None,
        )

    return drive


def _bot_typed(campaign: str, text: str, *, hours: bool) -> BotDriver:
    async def drive(s: AsyncSession) -> None:
        fsm = _fsm()
        waiting = (
            MarketingStates.waiting_offer_hours if hours else MarketingStates.waiting_offer_percent
        )
        await fsm.set_state(waiting)
        await fsm.update_data(mkt_campaign=campaign)
        handler = ma.marketing_typed_hours if hours else ma.marketing_typed_percent
        await handler(_typed(text), fsm, s, _owner_settings(), None)

    return drive


class Case:
    def __init__(
        self,
        campaign: str,
        action: str,
        form: dict[str, str],
        bot: BotDriver,
        *,
        prior: str | None = None,
    ) -> None:
        self.campaign, self.action, self.form, self.bot, self.prior = (
            campaign,
            action,
            form,
            bot,
            prior,
        )

    def __repr__(self) -> str:
        return f"{self.campaign}/{self.action}:{sorted(self.form.items())}:{self.prior}"


CASES = [
    Case("cart", "toggle", {"to": "on"}, _bot_toggle("cart")),
    Case("trial_convert", "toggle", {"to": "on"}, _bot_toggle("trial_convert")),
    Case("winback", "toggle", {"to": "on"}, _bot_toggle("winback")),
    Case("upgrade", "toggle", {"to": "on"}, _bot_toggle("upgrade")),
    Case("winback", "toggle", {"to": "off"}, _bot_toggle("winback"), prior="winback"),
    Case(
        "trial_convert",
        "percent",
        {"percent": "15"},
        _bot_typed("trial_convert", "15", hours=False),
    ),
    Case("winback", "percent", {"percent": "۲۰"}, _bot_typed("winback", "۲۰", hours=False)),
    Case(
        "upgrade", "percent", {"percent": "0"}, _bot_typed("upgrade", "0", hours=False), prior="x"
    ),
    Case(
        "trial_convert",
        "hours",
        {"hours": "24"},
        _bot_typed("trial_convert", "24", hours=True),
    ),
    Case("upgrade", "hours", {"hours": "۱۶۸"}, _bot_typed("upgrade", "۱۶۸", hours=True)),
]


async def _prior(factory: async_sessionmaker[AsyncSession], prior: str | None) -> None:
    """A state to start from: a campaign already on (a name), or an offer already set ('x')."""
    if prior is None:
        return
    async with factory() as s:
        if prior == "x":
            await set_offer_percent(s, campaign="upgrade", percent=30)
        else:
            await set_campaign(s, campaign=prior, on=True)
        await s.commit()


@pytest_asyncio.fixture
async def twin(
    two_sessions: Factory, tmp_path: Path
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]]:
    web_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield web_factory, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_web_write_leaves_the_row_the_bots_handler_leaves(
    twin, case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, bot_factory = twin
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ma, "_tg_message", lambda _cb: screen)
    for factory in (web_factory, bot_factory):
        await _prior(factory, case.prior)
    before = await _row(web_factory)

    async with bot_factory() as s:
        await case.bot(s)

    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, case.campaign, case.action, case.form, seen=await _token(web_factory)
        )
    assert status == 200, html
    after = await _row(web_factory)
    assert (
        after != before
    ), "the write must have changed something for the comparison to mean anything"
    assert after == await _row(bot_factory)
    assert texts.admin_mkt_saved in html


# --------------------------------------------------------------------------- #
# Ships off; looking and defaults switch nothing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_campaign_ships_off_and_looking_writes_nothing(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
        again = await _page(client)
    assert html == again
    assert await _rows(fresh) == 0, "looking at the page does not create the row"
    for name in CAMPAIGNS:
        card = _card(html, name)
        assert texts.admin_mkt_state_off in card and 'aria-checked="false"' in card
        assert 'name="to" value="on"' in card and 'value="off"' not in card
    assert "روشن از" not in html


@pytest.mark.asyncio
async def test_nothing_is_switched_on_by_a_missing_or_unknown_field(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        for name in CAMPAIGNS:
            for form in ({}, {"to": ""}, {"to": "yes"}, {"to": "1"}, {"to": "ON"}):
                status, html = await _post(client, name, "toggle", form, seen=await _token(fresh))
                assert status == 400 and texts.admin_action_error in html, (name, form)
    assert await _rows(fresh) == 0


@pytest.mark.asyncio
async def test_saving_an_offer_does_not_switch_the_campaign_on(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        await _post(client, "winback", "percent", {"percent": "10"}, seen=await _token(fresh))
        await _post(client, "winback", "hours", {"hours": "12"}, seen=await _token(fresh))
    row = await _row(fresh)
    assert row is not None and not any(row[f"{n}_on"] for n in CAMPAIGNS)


@pytest.mark.asyncio
async def test_a_percent_stays_editable_while_the_campaign_is_off(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
        for name in OFFER_CAMPAIGNS:
            assert f"/panel/marketing/{name}/percent" in _card(html, name), name
            assert f"/panel/marketing/{name}/hours" not in _card(
                html, name
            ), "hours wait for a percent"
        status, html = await _post(
            client, "trial_convert", "percent", {"percent": "15"}, seen=await _token(fresh)
        )
        assert status == 200
        row = await _row(fresh)
        assert row is not None and row["trial_convert_percent"] == 15
        assert row["trial_convert_on"] is False
        card = _card(html, "trial_convert")
        assert "/panel/marketing/trial_convert/hours" in card, "an offer shows its hours"
        assert texts.admin_mkt_offer_state.format(percent="۱۵", hours="۴۸") in card
        # and it can be changed again, still off
        status, _ = await _post(
            client, "trial_convert", "percent", {"percent": "0"}, seen=await _token(fresh)
        )
        assert status == 200
    row = await _row(fresh)
    assert (
        row is not None and row["trial_convert_percent"] == 0 and row["trial_convert_on"] is False
    )


@pytest.mark.asyncio
async def test_the_cart_reminder_has_no_offer_form_and_a_forged_one_is_refused(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
        assert "/panel/marketing/cart/percent" not in html
        assert "/panel/marketing/cart/hours" not in html
        for action, form in (("percent", {"percent": "10"}), ("hours", {"hours": "10"})):
            status, page = await _post(client, "cart", action, form, seen=await _token(fresh))
            assert status == 400 and texts.admin_mkt_offer_no_such_campaign in page
    assert await _rows(fresh) == 0


@pytest.mark.asyncio
async def test_an_unknown_campaign_is_refused(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        status, html = await _post(client, "nope", "toggle", {"to": "on"}, seen=await _token(fresh))
    assert status == 400 and "این پیام وجود ندارد." in html
    assert await _rows(fresh) == 0


# --------------------------------------------------------------------------- #
# The page is the bot's screen
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_page_carries_the_bots_titles_state_and_offer_lines(fresh) -> None:
    async with fresh() as s:
        await set_campaign(s, campaign="winback", on=True)
        await set_offer_percent(s, campaign="winback", percent=15)
        await s.commit()
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
    for title in ("👋 بعد از پایان اشتراک", "🛒 سفارش نیمه‌کاره", "🎁 بعد از تست"):
        assert title in html
    assert "روشن از" not in _card(html, "winback"), "the switch is the state"
    assert "🎁 تخفیف همراه پیام: ۱۵٪، ۴۸ ساعت اعتبار" in _card(html, "winback")
    assert "تخفیف بعد از پایان اشتراک: ۱۵٪" in _card(html, "winback")
    assert "۹۰ روز" in _card(html, "winback") or "روز بعد از پایان" in _card(html, "winback")
    intro = texts.admin_mkt_screen.format(lines="").strip().partition("\n\n")[2]
    assert intro.split("\n")[0] in html.replace("&#39;", "'")


@pytest.mark.asyncio
async def test_an_editor_sees_no_state_line_and_no_mark_a_viewer_still_sees_the_state(
    fresh,
) -> None:
    async with fresh() as s:
        await set_campaign(s, campaign="winback", on=True)
        await set_offer_percent(s, campaign="winback", percent=15)
        await s.commit()
    async with panel_client(fresh, _owner_settings()) as client:
        editor = await _page(client)
    async with panel_client(fresh, panel_settings()) as client:
        viewer = await _page(client)
    assert "camp-state" not in editor
    assert "✅" not in editor and "❌" not in editor
    assert "—" not in editor
    assert "روشن از" in _card(viewer, "winback")
    assert 'class="camp-state"' in viewer
    # the bot's keyboard still carries the marks
    from bot.keyboards.marketing import offer_percent_label, toggle_label

    assert toggle_label("winback", on=True).startswith("✅")
    assert offer_percent_label("winback", 0).startswith("❌")


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_reads_and_has_no_control(fresh) -> None:
    async with panel_client(fresh, panel_settings()) as client:
        html = await _page(client)
        assert texts.admin_mkt_not_owner in html
        assert "hx-post" not in html and 'name="seen"' not in html
        status, forged = await _post(
            client, "cart", "toggle", {"to": "on"}, seen=await _token(fresh)
        )
    assert status == 403 and texts.admin_mkt_not_owner in forged
    assert await _rows(fresh) == 0


# --------------------------------------------------------------------------- #
# Validation: the bot's parsing and lines, nothing written
# --------------------------------------------------------------------------- #

PERCENT_RANGE = texts.admin_mkt_offer_percent_range.format(high="۵۰")
HOURS_RANGE = texts.admin_mkt_offer_hours_range.format(high="۱۶۸")
BAD = [
    ("percent", {"percent": "51"}, PERCENT_RANGE),
    ("percent", {"percent": "-1"}, PERCENT_RANGE),
    ("percent", {"percent": "abc"}, PERCENT_RANGE),
    ("percent", {"percent": ""}, PERCENT_RANGE),
    ("percent", {"percent": "1.5"}, PERCENT_RANGE),
    ("percent", {"percent": "1234567"}, PERCENT_RANGE),
    ("percent", {"percent": NINES}, PERCENT_RANGE),
    ("percent", {"percent": "²"}, PERCENT_RANGE),
    ("hours", {"hours": "0"}, HOURS_RANGE),
    ("hours", {"hours": "169"}, HOURS_RANGE),
    ("hours", {"hours": "x"}, HOURS_RANGE),
    ("hours", {"hours": ""}, HOURS_RANGE),
    ("hours", {"hours": NINES}, HOURS_RANGE),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "form", "line"), BAD, ids=[f"{a}:{f}" for a, f, _ in BAD])
async def test_a_bad_number_is_refused_with_the_bots_line_and_nothing_is_written(
    fresh, action: str, form: dict[str, str], line: str
) -> None:
    async with fresh() as s:
        await set_offer_percent(s, campaign="winback", percent=10)
        await s.commit()
    before, token = await _row(fresh), await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        status, html = await _post(client, "winback", action, form, seen=token)
    assert status == 400, html
    assert texts.admin_mkt_offer_input_error.format(reason=line) in html
    assert await _row(fresh) == before and await _token(fresh) == token


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed", "ok"), [("0", True), ("50", True), ("51", False), ("٥", True)])
async def test_the_percent_bounds_are_the_bots(fresh, typed: str, ok: bool) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        status, _ = await _post(
            client, "winback", "percent", {"percent": typed}, seen=await _token(fresh)
        )
    assert (status == 200) is ok


# --------------------------------------------------------------------------- #
# A form of its own: stale and double-submitted
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_toggle_sent_twice_applies_once_and_never_flips_back(fresh) -> None:
    token = await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        first, _ = await _post(client, "winback", "toggle", {"to": "on"}, seen=token)
        row_after_first = await _row(fresh)
        second, html = await _post(client, "winback", "toggle", {"to": "on"}, seen=token)
    assert (first, second) == (200, 409)
    assert TEXT_STALE_MARKETING in html
    assert await _row(fresh) == row_after_first
    assert row_after_first is not None and row_after_first["winback_on"] is True


@pytest.mark.asyncio
async def test_the_off_form_sent_twice_does_not_switch_it_back_on(fresh) -> None:
    async with fresh() as s:
        await set_campaign(s, campaign="winback", on=True)
        await s.commit()
    token = await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        results = [
            (await _post(client, "winback", "toggle", {"to": "off"}, seen=token))[0]
            for _ in range(2)
        ]
    assert results == [200, 409]
    row = await _row(fresh)
    assert row is not None and row["winback_on"] is False


@pytest.mark.asyncio
async def test_two_toggles_at_the_same_moment_apply_once(fresh) -> None:
    token = await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        results = await asyncio.gather(
            _post(client, "cart", "toggle", {"to": "on"}, seen=token),
            _post(client, "cart", "toggle", {"to": "on"}, seen=token),
        )
    assert sorted(status for status, _ in results) == [200, 409]
    row = await _row(fresh)
    assert row is not None and row["cart_on"] is True


@pytest.mark.asyncio
async def test_a_form_drawn_before_another_edit_is_refused_and_nothing_is_merged(fresh) -> None:
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
        drawn = re.search(r'name="seen" value="([^"]*)"', html)
        assert drawn is not None
        # the bot (or another tab) changes a setting meanwhile
        async with fresh() as s:
            await set_offer_percent(s, campaign="upgrade", percent=25)
            await s.commit()
        before = await _row(fresh)
        status, page = await _post(
            client, "winback", "percent", {"percent": "10"}, seen=drawn.group(1)
        )
        assert status == 409 and TEXT_STALE_MARKETING in page and "بارگذاری دوباره" in page
        assert await _row(fresh) == before
        # reloaded, the same edit goes through
        status, _ = await _post(
            client, "winback", "percent", {"percent": "10"}, seen=await _token(fresh)
        )
        assert status == 200


@pytest.mark.asyncio
async def test_every_form_on_the_page_carries_the_token_it_was_drawn_from(fresh) -> None:
    async with fresh() as s:
        await set_offer_percent(s, campaign="winback", percent=5)
        await s.commit()
    token = await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        html = await _page(client)
    assert set(re.findall(r'name="seen" value="([^"]*)"', html)) == {token}
    assert html.count('name="seen"') >= 4 + 3 + 1


@pytest.mark.asyncio
async def test_the_page_after_a_save_carries_the_new_token(fresh) -> None:
    old = await _token(fresh)
    async with panel_client(fresh, _owner_settings()) as client:
        _status, html = await _post(client, "winback", "toggle", {"to": "on"}, seen=old)
    new = await _token(fresh)
    assert new != old
    assert set(re.findall(r'name="seen" value="([^"]*)"', html)) == {new}


# --------------------------------------------------------------------------- #
# One guard for every write route
# --------------------------------------------------------------------------- #

ROUTES = {
    "/panel/marketing/{campaign}/toggle": ("winback", "toggle", {"to": "on"}),
    "/panel/marketing/{campaign}/percent": ("winback", "percent", {"percent": "10"}),
    "/panel/marketing/{campaign}/hours": ("winback", "hours", {"hours": "10"}),
}


@pytest.mark.asyncio
async def test_every_post_route_under_marketing_has_a_guard_case(fresh) -> None:
    """A write route added later with no entry in ROUTES fails here."""
    async with panel_client(fresh, _owner_settings()) as client:
        posts = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "POST"
            and r.resource
            and r.resource.canonical.startswith("/panel/marketing")
        }
    assert posts, "the marketing page has write routes"
    assert posts == set(ROUTES), f"uncovered: {posts - set(ROUTES)}, unknown: {set(ROUTES) - posts}"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_a_write_needs_login_admin_owner_and_the_current_token(fresh, route: str) -> None:
    campaign, action, form = ROUTES[route]
    url = f"/panel/marketing/{campaign}/{action}"
    token = await _token(fresh)
    data = {**form, "seen": token}
    async with panel_client(fresh, _owner_settings()) as client:
        assert (await client.post(url, data=data)).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (await client.post(url, data=data, headers=stranger)).status == 403
        # GET never writes (and the write routes have no GET)
        assert (await client.get(url, headers=OWNER)).status == 405
        assert (await client.get(f"{url}?seen={token}", headers=OWNER)).status == 405
        # a wrong token, and no token at all
        resp = await client.post(url, data={**form, "seen": "yesterday"}, headers=OWNER)
        assert resp.status == 409 and TEXT_STALE_MARKETING in await resp.text()
        resp = await client.post(url, data=form, headers=OWNER)
        assert resp.status == 409, "no token at all is as stale as a wrong one"
    assert await _rows(fresh) == 0

    # an admin who is not a store owner reads but cannot write
    async with panel_client(fresh, panel_settings()) as client:
        resp = await client.post(url, data=data, headers=OWNER)
        assert resp.status == 403 and texts.admin_mkt_not_owner in await resp.text()
    assert await _rows(fresh) == 0
