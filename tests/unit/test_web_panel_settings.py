"""«⚙️ تنظیمات» on the Mini App: the settings home and its four sections.

What these defend, each with a test that goes red when the real line is broken:

* a lifecycle write on the web leaves the row the bot's own handler leaves for the
  same input (parity, every setting and every mode, against the bot's real handlers
  on a second database);
* each list and number refuses out-of-range, non-numeric, empty and beyond-64-bit
  input with the bot's text and writes nothing;
* real deletion cannot be switched on without a fresh rehearsal, on the first
  request or the confirm one, and the confirm page writes nothing;
* every POST under ``/panel/settings`` obeys one guard (login, admin, store owner,
  current stamp, GET writes nothing, a double submit applies once), a route added
  later with no case here fails the build, and customers and resellers are refused
  on every settings path;
* no settings page renders a secret;
* the report group, panel connection and QR background pages are read-only: no
  form, no POST route, and looking writes no row;
* the figures on the pages are what the services return for the seeded rows.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from bot.handlers import lifecycle_admin
from bot.keyboards.lifecycle import LifeCB
from bot.states import LifecycleStates
from bot.texts import texts
from database.base import Base
from database.models.brand_asset import BrandAsset
from database.models.lifecycle import (
    DELETE_MODE_OFF,
    DELETE_MODE_ON,
    DELETE_MODE_REHEARSAL,
    LIFECYCLE_SETTINGS_ROW_ID,
    LifecycleBatch,
    LifecycleSettings,
)
from database.models.panel_node_state import PANEL_NODE_ID, PanelNodeState
from database.models.pasarguard_settings import PasarGuardSettings
from database.models.report import ReportSettings
from services.lifecycle_settings import (
    MAX_DAYS,
    NUMBER_FIELDS,
    TEXT_STALE,
    SettingsError,
    current_stamp,
    load_config,
    parse_day_list,
    parse_days_number,
    parse_field,
    parse_percent_list,
)
from services.lifecycle_view import FIELD_LABELS, current_value_text, screen_text
from services.qr_brand import QR_BACKGROUND_KEY
from services.report_group import TOPIC_KEYS, get_readiness
from services.settings_overview import overview
from tests.unit.web_panel_helpers import (
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    TOKEN,
    add_user,
    init_data,
    panel_client,
    panel_settings,
)
from tests.unit.web_personal_helpers import make_person, personal_client

Factory = tuple[async_sessionmaker[AsyncSession], str]
OWNER = {INIT_HEADER: init_data(ENV_ADMIN)}
SEED_DAYS = [3, 1]
NINES = "9" * 30
HUGE = "9" * 5000  # past Python's own 4300-digit int() limit
CUSTOMER, RESELLER = 4001, 4002

LIFECYCLE = "/panel/settings/lifecycle"
READ_ONLY_PAGES = ("/panel/settings/panel", "/panel/settings/reports", "/panel/settings/qr")
ALL_GET_PAGES = ("/panel/settings", LIFECYCLE, *READ_ONLY_PAGES)


def _owner_settings(**more: object) -> Any:
    return panel_settings(STORE_OWNER_TELEGRAM_IDS=str(ENV_ADMIN), **more)


# --------------------------------------------------------------------------- #
# Seeds and snapshots
# --------------------------------------------------------------------------- #


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    mode: str = DELETE_MODE_OFF,
    rehearsal: datetime | None = None,
    warn_days: tuple[int, ...] = (3, 1),
    percents: tuple[int, ...] = (20, 5),
) -> None:
    async with factory() as s:
        s.add(
            LifecycleSettings(
                id=LIFECYCLE_SETTINGS_ROW_ID,
                warn_days=list(warn_days),
                warn_volume_percents=list(percents),
                delete_mode=mode,
                delete_after_expiry_days=10,
                delete_after_limited_days=10,
                delete_trial_after_days=1,
                delete_warn_days_before=3,
                last_rehearsal_at=rehearsal,
            )
        )
        await s.commit()


async def _row(factory: async_sessionmaker[AsyncSession]) -> dict[str, object] | None:
    """The settings row without its ``updated_at`` (two databases cannot share a clock)."""
    async with factory() as s:
        row = await s.get(LifecycleSettings, LIFECYCLE_SETTINGS_ROW_ID)
        if row is None:
            return None
        rehearsal = row.last_rehearsal_at
        return {
            "warn_days": list(row.warn_days),
            "warn_volume_percents": list(row.warn_volume_percents),
            "delete_mode": row.delete_mode,
            "delete_after_expiry_days": row.delete_after_expiry_days,
            "delete_after_limited_days": row.delete_after_limited_days,
            "delete_trial_after_days": row.delete_trial_after_days,
            "delete_warn_days_before": row.delete_warn_days_before,
            "last_rehearsal_at": rehearsal.replace(tzinfo=None) if rehearsal else None,
        }


async def _full_row(factory: async_sessionmaker[AsyncSession]) -> tuple[object, ...] | None:
    """Every column, ``updated_at`` included: «nothing was written» means this did not move."""
    async with factory() as s:
        row = await s.get(LifecycleSettings, LIFECYCLE_SETTINGS_ROW_ID)
        if row is None:
            return None
        return tuple(getattr(row, c.name) for c in LifecycleSettings.__table__.columns)


async def _stamp(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as s:
        return await current_stamp(s)


async def _count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


def _fresh(days: float = 1) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@pytest_asyncio.fixture
async def shop(two_sessions: Factory) -> async_sessionmaker[AsyncSession]:
    """A shop with a settings row: deletion in rehearsal, rehearsed a day ago."""
    factory, _ = two_sessions
    await _seed(factory, mode=DELETE_MODE_REHEARSAL, rehearsal=_fresh())
    return factory


async def _post(
    client: TestClient,
    url: str,
    form: dict[str, str],
    *,
    stamp: str | None,
    headers: dict[str, str] = OWNER,
) -> tuple[int, str]:
    data = dict(form)
    if stamp is not None:
        data["stamp"] = stamp
    resp = await client.post(url, data=data, headers=headers)
    return resp.status, await resp.text()


async def _get(client: TestClient, url: str, headers: dict[str, str] = OWNER) -> tuple[int, str]:
    resp = await client.get(url, headers=headers)
    return resp.status, await resp.text()


def _number_url(field: str) -> str:
    return f"{LIFECYCLE}/number/{field}"


MODE_URL = f"{LIFECYCLE}/mode"


# --------------------------------------------------------------------------- #
# The bot's own handlers, driven on a second database
# --------------------------------------------------------------------------- #


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ENV_ADMIN, user_id=ENV_ADMIN)
    )


def _bot_settings() -> Any:
    return _owner_settings()


async def bot_types(factory: async_sessionmaker[AsyncSession], field: str, text: str) -> str:
    """Type ``text`` into the bot's prompt for ``field``; returns the bot's reply."""
    fsm = _fsm()
    await fsm.set_state(LifecycleStates.waiting_value)
    await fsm.update_data(life_field=field)
    message = SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=ENV_ADMIN, is_bot=False), answer=AsyncMock()
    )
    async with factory() as s:
        await lifecycle_admin.lifecycle_typed_value(message, fsm, s, _bot_settings(), None)
    return str(message.answer.await_args.args[0])


async def bot_taps_mode(
    factory: async_sessionmaker[AsyncSession], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot's mode button; for «روشن» the confirm button after it."""
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(lifecycle_admin, "_tg_message", lambda _cb: screen)

    def callback(data: str) -> SimpleNamespace:
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=ENV_ADMIN), answer=AsyncMock(), message=None
        )

    async with factory() as s:
        await lifecycle_admin.lifecycle_mode(
            callback(f"{LifeCB.MODE}{mode}"), _fsm(), s, _bot_settings(), None
        )
    if mode == DELETE_MODE_ON:
        async with factory() as s:
            await lifecycle_admin.lifecycle_mode_on_confirmed(
                callback(LifeCB.MODE_ON_OK), _fsm(), s, _bot_settings(), None
            )


@pytest_asyncio.fixture
async def twin(
    two_sessions: Factory, tmp_path: Path
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]]:
    """The web's database and a second one for the bot's path (seeded by each test)."""
    web_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield web_factory, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _text_of(html: str) -> str:
    """The page's words: tags gone, entities decoded, whitespace folded."""
    import html as html_lib

    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _fold(text: str) -> str:
    return " ".join(text.split())


def _has_lines(page: str, bot_text: str) -> None:
    """Every line the bot printed is on the page (whitespace folded)."""
    folded = _text_of(page)
    for raw_line in bot_text.splitlines():
        line = " ".join(raw_line.split())
        if line:
            assert line in folded, f"the bot says «{line}» and the page does not"


# --------------------------------------------------------------------------- #
# Parity: the web leaves the row the bot's handler leaves
# --------------------------------------------------------------------------- #

NUMBER_CASES = [
    ("warn_days", "5 2 1"),
    ("warn_volume_percents", "50 10"),
    ("delete_after_expiry_days", "14"),
    ("delete_after_limited_days", "8"),
    ("delete_trial_after_days", "2"),
    ("delete_warn_days_before", "4"),
    ("warn_days", "۷ ۲"),  # Persian digits
    ("warn_volume_percents", "0"),  # off
    ("delete_after_expiry_days", "0"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "typed"), NUMBER_CASES)
@pytest.mark.parametrize("mode", [DELETE_MODE_ON, DELETE_MODE_REHEARSAL])
async def test_a_web_save_leaves_the_row_the_bots_prompt_leaves(
    twin, field: str, typed: str, mode: str
) -> None:
    web_factory, bot_factory = twin
    rehearsal = _fresh()
    for factory in (web_factory, bot_factory):
        await _seed(factory, mode=mode, rehearsal=rehearsal)

    reply = await bot_types(bot_factory, field, typed)

    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, _number_url(field), {"value": typed}, stamp=await _stamp(web_factory)
        )
    assert status == 200, html
    after = await _row(web_factory)
    assert after == await _row(bot_factory)
    assert after != {**after, field: object()}  # (the dict is a real row)
    # The saved line, and what a deletion number did to the mode, are the bot's words.
    saved = reply.split("\n\n")[0]
    assert saved.startswith(texts.admin_life_saved)
    _has_lines(html, saved)
    # …and the page then shows the same figures as the bot's screen under that line.
    async with web_factory() as s:
        config = await load_config(s, seed_days=SEED_DAYS)
    _has_figures(html, screen_text(config))


def _has_figures(html: str, bot_screen: str) -> None:
    folded = _text_of(html)
    for line in bot_screen.splitlines():
        label, sep, value = line.partition(": ")
        if sep and label in dict(FIELD_LABELS).values():
            assert label in folded and value.strip() in folded, line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "target"),
    [
        (DELETE_MODE_OFF, DELETE_MODE_REHEARSAL),
        (DELETE_MODE_REHEARSAL, DELETE_MODE_OFF),
        (DELETE_MODE_ON, DELETE_MODE_REHEARSAL),
        (DELETE_MODE_ON, DELETE_MODE_OFF),
        (DELETE_MODE_REHEARSAL, DELETE_MODE_ON),
    ],
)
async def test_a_web_mode_switch_leaves_the_row_the_bots_button_leaves(
    twin, monkeypatch: pytest.MonkeyPatch, start: str, target: str
) -> None:
    web_factory, bot_factory = twin
    rehearsal = _fresh()
    for factory in (web_factory, bot_factory):
        await _seed(factory, mode=start, rehearsal=rehearsal)

    await bot_taps_mode(bot_factory, target, monkeypatch)

    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, MODE_URL, {"mode": target}, stamp=await _stamp(web_factory)
        )
        if target == DELETE_MODE_ON:
            assert status == 200 and _fold(texts.admin_life_mode_confirm) in _text_of(html)
            status, html = await _post(
                client,
                MODE_URL,
                {"mode": target, "confirm": "1"},
                stamp=await _stamp(web_factory),
            )
    assert status == 200, html
    assert (await _row(web_factory)) == (await _row(bot_factory))
    assert (await _row(web_factory))["delete_mode"] == target  # type: ignore[index]
    assert texts.admin_life_mode_set.format(mode=texts.admin_life_mode_names[target]) in _text_of(
        html
    )


@pytest.mark.asyncio
async def test_the_first_save_of_a_shop_with_no_row_creates_the_row_the_bot_creates(twin) -> None:
    web_factory, bot_factory = twin
    reply = await bot_types(bot_factory, "warn_days", "9 4")
    assert texts.admin_life_saved in reply
    async with panel_client(web_factory, _owner_settings()) as client:
        status, _ = await _post(
            client, _number_url("warn_days"), {"value": "9 4"}, stamp=await _stamp(web_factory)
        )
    assert status == 200
    assert (await _row(web_factory)) == (await _row(bot_factory)) is not None


@pytest.mark.asyncio
async def test_a_form_drawn_before_the_row_existed_is_stale_once_the_row_does(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    async with panel_client(factory, _owner_settings()) as client:
        stamp = await _stamp(factory)
        assert stamp == "none"
        first, _ = await _post(client, _number_url("warn_days"), {"value": "9"}, stamp=stamp)
        second, html = await _post(client, _number_url("warn_days"), {"value": "8"}, stamp=stamp)
    assert (first, second) == (200, 409) and TEXT_STALE in html
    assert (await _row(factory))["warn_days"] == [9]  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Validation: the bot's text, nothing written
# --------------------------------------------------------------------------- #

LIST_BAD = ["abc", "1 x", "3.5", "-1", "0 3", NINES, HUGE, "1,5x"]
NUMBER_BAD = ["", "   ", "abc", "-1", "1 2", "3.5", NINES, HUGE]
RANGE_BAD = {
    "warn_days": ["366", "400 2"],
    "warn_volume_percents": ["100", "99 100"],
    "delete_after_expiry_days": ["366"],
    "delete_after_limited_days": ["366"],
    "delete_trial_after_days": ["366"],
    "delete_warn_days_before": ["366"],
}


def _bad_inputs() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for field in NUMBER_FIELDS:
        base = LIST_BAD if field in ("warn_days", "warn_volume_percents") else NUMBER_BAD
        cases += [(field, raw) for raw in (*base, *RANGE_BAD[field])]
    return cases


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "raw"), _bad_inputs(), ids=lambda v: str(v)[:24])
async def test_a_bad_value_is_refused_in_the_bots_words_and_writes_nothing(
    twin, field: str, raw: str
) -> None:
    web_factory, bot_factory = twin
    rehearsal = _fresh()
    for factory in (web_factory, bot_factory):
        await _seed(factory, mode=DELETE_MODE_ON, rehearsal=rehearsal, warn_days=(7,))
    before = await _full_row(web_factory)

    reply = await bot_types(bot_factory, field, raw)  # what the bot says to the same text
    assert reply.startswith("❌"), reply

    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, _number_url(field), {"value": raw}, stamp=await _stamp(web_factory)
        )
    assert status == 400
    assert " ".join(reply.split()) in _text_of(html)
    assert await _full_row(web_factory) == before, "a refused value wrote something"
    assert (await _row(web_factory)) == (await _row(bot_factory))


@pytest.mark.parametrize("parse", [parse_day_list, parse_percent_list, parse_days_number])
@pytest.mark.parametrize("raw", [NINES, HUGE, "9" * 4301])
def test_a_beyond_64_bit_number_is_a_refusal_never_a_value_error(parse: Any, raw: str) -> None:
    with pytest.raises(SettingsError):
        parse(raw)


def test_a_field_parses_with_the_parser_its_screen_uses() -> None:
    assert parse_field("warn_days", "۳ ۱") == [3, 1]
    assert parse_field("warn_volume_percents", "20 5") == [20, 5]
    assert parse_field("delete_trial_after_days", "0") == 0
    with pytest.raises(SettingsError):
        parse_field("delete_mode", "1")
    # each field has its own range: a day list takes 100, a percent list does not
    assert parse_field("warn_days", "100") == [100]
    with pytest.raises(SettingsError):
        parse_field("warn_volume_percents", "100")
    with pytest.raises(SettingsError):
        parse_field("warn_days", "366")


@pytest.mark.asyncio
async def test_reading_the_readiness_for_a_page_leaves_no_row_even_if_the_caller_commits(
    two_sessions: Factory,
) -> None:
    """``create=False`` is what lets a page look at a quiet shop; the bot's own screen
    (the default) creates the row, as before."""
    factory, _ = two_sessions
    async with factory() as s:
        readiness = await get_readiness(s, create=False)
        await s.commit()
    assert not readiness.attached and readiness.enabled is False
    assert await _count(factory, ReportSettings) == 0
    async with factory() as s:
        await get_readiness(s)
        await s.commit()
    assert await _count(factory, ReportSettings) == 1


@pytest.mark.asyncio
async def test_a_field_that_is_not_a_setting_is_not_found(shop) -> None:
    before = await _full_row(shop)
    async with panel_client(shop, _owner_settings()) as client:
        status, _ = await _post(
            client, _number_url("delete_mode"), {"value": "1"}, stamp=await _stamp(shop)
        )
        status2, _ = await _post(
            client, _number_url("last_rehearsal_at"), {"value": "1"}, stamp=await _stamp(shop)
        )
    assert (status, status2) == (404, 404)
    assert await _full_row(shop) == before


# --------------------------------------------------------------------------- #
# Real deletion needs a fresh rehearsal, everywhere it can be asked
# --------------------------------------------------------------------------- #

NO_REHEARSAL = [
    pytest.param(None, id="never_rehearsed"),
    pytest.param(timedelta(days=8), id="rehearsal_a_week_old"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("age", NO_REHEARSAL)
async def test_deletion_cannot_be_switched_on_without_a_fresh_rehearsal(
    two_sessions: Factory, age: timedelta | None
) -> None:
    factory, _ = two_sessions
    await _seed(
        factory,
        mode=DELETE_MODE_REHEARSAL,
        rehearsal=None if age is None else datetime.now(UTC) - age,
    )
    before = await _full_row(factory)
    async with panel_client(factory, _owner_settings()) as client:
        # asking is refused up front, with the bot's reason…
        status, html = await _post(client, MODE_URL, {"mode": "on"}, stamp=await _stamp(factory))
        assert status == 400
        assert "روشن کردن حذف خودکار رد شد" in _text_of(html)
        assert _fold(texts.admin_life_mode_confirm) not in _text_of(html)
        # …and so is a forged confirm that skips the asking
        status, html = await _post(
            client, MODE_URL, {"mode": "on", "confirm": "1"}, stamp=await _stamp(factory)
        )
        assert status == 400 and "روشن کردن حذف خودکار رد شد" in _text_of(html)
    assert await _full_row(factory) == before
    assert (await _row(factory))["delete_mode"] != DELETE_MODE_ON  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_refusal_is_the_bots_refusal(twin, monkeypatch: pytest.MonkeyPatch) -> None:
    web_factory, bot_factory = twin
    for factory in (web_factory, bot_factory):
        await _seed(factory, mode=DELETE_MODE_REHEARSAL, rehearsal=None)
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(lifecycle_admin, "_tg_message", lambda _cb: screen)
    cb = SimpleNamespace(
        data=f"{LifeCB.MODE}on",
        from_user=SimpleNamespace(id=ENV_ADMIN),
        answer=AsyncMock(),
        message=None,
    )
    async with bot_factory() as s:
        await lifecycle_admin.lifecycle_mode(cb, _fsm(), s, _bot_settings(), None)
    bot_says = str(screen.edit_text.await_args.args[0])
    assert bot_says.startswith("❌")
    async with panel_client(web_factory, _owner_settings()) as client:
        _, html = await _post(client, MODE_URL, {"mode": "on"}, stamp=await _stamp(web_factory))
    assert " ".join(bot_says.split()) in _text_of(html)


@pytest.mark.asyncio
async def test_the_confirm_page_writes_nothing_and_only_the_second_request_switches_deletion_on(
    shop,
) -> None:
    before = await _full_row(shop)
    stamp = await _stamp(shop)
    async with panel_client(shop, _owner_settings()) as client:
        status, html = await _post(client, MODE_URL, {"mode": "on"}, stamp=stamp)
        assert status == 200
        assert _fold(texts.admin_life_mode_confirm) in _text_of(html)
        assert texts.admin_life_mode_confirm_yes in _text_of(html)
        assert await _full_row(shop) == before, "asking must write nothing, not even updated_at"
        status, html = await _post(client, MODE_URL, {"mode": "on", "confirm": "1"}, stamp=stamp)
    assert status == 200
    assert (await _row(shop))["delete_mode"] == DELETE_MODE_ON  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_confirm_after_the_numbers_changed_meanwhile_does_not_switch_deletion_on(
    shop,
) -> None:
    """Ask, then another tab changes a deletion number (voiding the rehearsal): the
    confirm still carries the old stamp, so it is refused as stale."""
    stamp = await _stamp(shop)
    async with panel_client(shop, _owner_settings()) as client:
        await _post(client, MODE_URL, {"mode": "on"}, stamp=stamp)
        status, _ = await _post(
            client, _number_url("delete_after_expiry_days"), {"value": "20"}, stamp=stamp
        )
        assert status == 200
        status, html = await _post(client, MODE_URL, {"mode": "on", "confirm": "1"}, stamp=stamp)
    assert status == 409 and TEXT_STALE in html
    assert (await _row(shop))["delete_mode"] == DELETE_MODE_REHEARSAL  # type: ignore[index]


@pytest.mark.asyncio
async def test_asking_for_on_when_it_is_already_on_changes_nothing(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await _seed(factory, mode=DELETE_MODE_ON, rehearsal=_fresh())
    before = await _full_row(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(client, MODE_URL, {"mode": "on"}, stamp=await _stamp(factory))
    assert status == 200 and _fold(texts.admin_life_mode_confirm) not in _text_of(html)
    assert await _full_row(factory) == before


@pytest.mark.asyncio
async def test_a_mode_that_is_not_one_is_refused(shop) -> None:
    before = await _full_row(shop)
    async with panel_client(shop, _owner_settings()) as client:
        status, html = await _post(
            client, MODE_URL, {"mode": "delete-all"}, stamp=await _stamp(shop)
        )
    assert status == 400 and "این حالت وجود ندارد" in html
    assert await _full_row(shop) == before


# --------------------------------------------------------------------------- #
# One guard for every write route
# --------------------------------------------------------------------------- #

GUARD_CASES: list[tuple[str, str, dict[str, str]]] = [
    # (canonical route, concrete URL, form); the seed differs from every form, so a write shows
    ("/panel/settings/lifecycle/number/{field}", _number_url("warn_days"), {"value": "9 4"}),
    (
        "/panel/settings/lifecycle/number/{field}",
        _number_url("warn_volume_percents"),
        {"value": "50"},
    ),
    (
        "/panel/settings/lifecycle/number/{field}",
        _number_url("delete_after_expiry_days"),
        {"value": "21"},
    ),
    (
        "/panel/settings/lifecycle/number/{field}",
        _number_url("delete_after_limited_days"),
        {"value": "21"},
    ),
    (
        "/panel/settings/lifecycle/number/{field}",
        _number_url("delete_trial_after_days"),
        {"value": "3"},
    ),
    (
        "/panel/settings/lifecycle/number/{field}",
        _number_url("delete_warn_days_before"),
        {"value": "5"},
    ),
    ("/panel/settings/lifecycle/mode", MODE_URL, {"mode": "off"}),
]
CASE_IDS = [url.removeprefix(LIFECYCLE) for _, url, _ in GUARD_CASES]


@pytest.mark.asyncio
async def test_every_post_route_under_settings_has_a_guard_case(shop) -> None:
    """A write route added later with no case in GUARD_CASES fails the build."""
    covered = {canonical for canonical, *_ in GUARD_CASES}
    async with panel_client(shop, _owner_settings()) as client:
        posts = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "POST"
            and r.resource
            and r.resource.canonical.startswith("/panel/settings")
        }
    assert posts, "the settings page has write routes"
    assert posts == covered, f"uncovered: {posts - covered}, unknown: {covered - posts}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("canonical", "url", "form"), GUARD_CASES, ids=CASE_IDS)
async def test_a_settings_write_needs_login_admin_owner_and_the_current_stamp(
    shop, canonical: str, url: str, form: dict[str, str]
) -> None:
    stamp = await _stamp(shop)
    full = {**form, "stamp": stamp}
    before = await _full_row(shop)
    async with panel_client(shop, _owner_settings()) as client:
        assert (await client.post(url, data=full)).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (await client.post(url, data=full, headers=stranger)).status == 403
        # GET never writes (and the route has no GET)
        assert (await client.get(url, headers=OWNER)).status == 405
        assert (await client.get(f"{url}?stamp={stamp}", headers=OWNER)).status == 405
        # a form drawn from another state of the row, or from none at all
        status, html = await _post(client, url, form, stamp="2001-01-01T00:00:00+00:00")
        assert status == 409 and TEXT_STALE in html
        status, _ = await _post(client, url, form, stamp=None)
        assert status == 409, "no stamp at all is as stale as a wrong one"
    assert await _full_row(shop) == before

    # an admin who is not a store owner reads but cannot write
    async with panel_client(shop, panel_settings()) as client:
        status, html = await _post(client, url, form, stamp=stamp)
        assert status == 403 and texts.admin_life_not_owner in html
    assert await _full_row(shop) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("canonical", "url", "form"), GUARD_CASES, ids=CASE_IDS)
async def test_a_settings_form_sent_twice_applies_once(
    shop, canonical: str, url: str, form: dict[str, str]
) -> None:
    # off -> off would change nothing to compare, so start the mode case from rehearsal
    stamp = await _stamp(shop)
    async with panel_client(shop, _owner_settings()) as client:
        first, _html = await _post(client, url, form, stamp=stamp)
        after_first = await _full_row(shop)
        second, html = await _post(client, url, form, stamp=stamp)
    assert (first, second) == (200, 409)
    assert TEXT_STALE in html
    assert await _full_row(shop) == after_first, "the second form changed nothing"


@pytest.mark.asyncio
async def test_a_mode_toggle_never_flips_back_when_its_form_is_sent_again(shop) -> None:
    """rehearsal -> off, then the same form again must not undo or redo anything."""
    stamp = await _stamp(shop)
    async with panel_client(shop, _owner_settings()) as client:
        await _post(client, MODE_URL, {"mode": "off"}, stamp=stamp)
        await _post(client, MODE_URL, {"mode": "rehearsal"}, stamp=await _stamp(shop))
        # the first form again (it asks for «off»): refused, the mode stays «rehearsal»
        status, _ = await _post(client, MODE_URL, {"mode": "off"}, stamp=stamp)
    assert status == 409
    assert (await _row(shop))["delete_mode"] == DELETE_MODE_REHEARSAL  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(("canonical", "url", "form"), GUARD_CASES[:2], ids=CASE_IDS[:2])
async def test_two_settings_forms_at_the_same_moment_apply_once(
    shop, canonical: str, url: str, form: dict[str, str]
) -> None:
    stamp = await _stamp(shop)
    async with panel_client(shop, _owner_settings()) as client:
        results = await asyncio.gather(
            _post(client, url, form, stamp=stamp), _post(client, url, form, stamp=stamp)
        )
    assert sorted(status for status, _ in results) == [200, 409]


# --------------------------------------------------------------------------- #
# Roles: a customer and a reseller are refused on every settings path
# --------------------------------------------------------------------------- #


def _settings_routes(client: TestClient) -> list[tuple[str, str]]:
    found = []
    for route in client.app.router.routes():
        resource = route.resource
        if route.method != "HEAD" and resource and resource.canonical.startswith("/panel/settings"):
            url = resource.canonical.replace("{field}", "warn_days")
            found.append((route.method, url))
    return found


@pytest.mark.asyncio
async def test_a_customer_and_a_reseller_are_refused_on_every_settings_path(shop) -> None:
    await make_person(shop, CUSTOMER, name="مشتری")
    await make_person(shop, RESELLER, name="نماینده", reseller=True)
    before = await _full_row(shop)
    async with personal_client(
        shop, public=True, settings=_owner_settings(WEB_PANEL_PUBLIC_ENABLED=True)
    ) as client:
        routes = _settings_routes(client)
        assert {m for m, _ in routes} == {"GET", "POST"} and len(routes) >= 7
        for tg in (CUSTOMER, RESELLER):
            headers = {INIT_HEADER: init_data(tg)}
            for method, url in routes:
                resp = await client.request(
                    method, url, headers=headers, data={"value": "5", "mode": "off", "stamp": "x"}
                )
                assert resp.status == 403, (tg, method, url)
        # and with no signature at all
        for method, url in routes:
            assert (await client.request(method, url)).status == 401, (method, url)
    assert await _full_row(shop) == before


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_reads_the_numbers_and_gets_no_control(
    shop,
) -> None:
    async with panel_client(shop, panel_settings()) as client:
        status, html = await _get(client, LIFECYCLE)
    assert status == 200
    assert texts.admin_life_not_owner in html
    assert "hx-post" not in html and "<form" not in html and "<input" not in html
    for _, label in FIELD_LABELS:
        assert label in html


@pytest.mark.asyncio
async def test_the_owner_gets_a_form_per_number_and_the_three_modes(shop) -> None:
    async with panel_client(shop, _owner_settings()) as client:
        _, html = await _get(client, LIFECYCLE)
    for field, _ in FIELD_LABELS:
        assert f'hx-post="{_number_url(field)}"' in html
    assert html.count(f'hx-post="{MODE_URL}"') == 3
    assert f'value="{await _stamp(shop)}"' in html


# --------------------------------------------------------------------------- #
# Looking writes nothing; the read-only pages have no way to write
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_looking_at_every_settings_page_writes_no_row(
    two_sessions: Factory, owner: bool
) -> None:
    factory, _ = two_sessions  # a quiet shop: no settings rows at all
    async with panel_client(factory, _owner_settings() if owner else panel_settings()) as client:
        for url in ALL_GET_PAGES:
            status, _ = await _get(client, url)
            assert status == 200, url
    for model in (LifecycleSettings, ReportSettings, PasarGuardSettings, BrandAsset):
        assert await _count(factory, model) == 0, model.__name__


@pytest.mark.asyncio
async def test_the_read_only_sections_have_no_post_route_and_no_form(shop) -> None:
    async with panel_client(shop, _owner_settings()) as client:
        routes = _settings_routes(client)
        for page in READ_ONLY_PAGES:
            assert [m for m, u in routes if u == page] == ["GET"], page
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                resp = await client.request(method, page, headers=OWNER, data={"x": "1"})
                assert resp.status == 405, (method, page)
            _, html = await _get(client, page)
            for needle in ("<form", "<input", "<button", "<textarea", "<select", "hx-post"):
                assert needle not in html, (page, needle)
        # nothing in the panel mentions an upload, a reset or a test for them
        assert not [u for m, u in routes if m == "POST" and not u.startswith(LIFECYCLE)]


def _links(html: str) -> set[str]:
    """Every address a page can ask for (htmx get or post)."""
    return set(re.findall(r'hx-(?:get|post)="([^"]+)"', html))


ALLOWED_LINKS = {
    "/panel/settings",
    LIFECYCLE,
    *READ_ONLY_PAGES,
    MODE_URL,
    *(_number_url(field) for field in NUMBER_FIELDS),
}


@pytest.mark.asyncio
async def test_the_settings_pages_offer_no_way_to_delete_a_service_or_run_a_rehearsal(
    two_sessions: Factory,
) -> None:
    """Whatever a page links to or posts to is one of the settings routes: nothing for
    the preview, the deletion list, its approval, an exemption or the rehearsal run."""
    factory, _ = two_sessions
    await _seed(factory, mode=DELETE_MODE_ON, rehearsal=_fresh())
    async with factory() as s:
        s.add(LifecycleBatch(kind="stop", status="open", numbers_key="k"))
        await s.commit()
    async with panel_client(factory, _owner_settings()) as client:
        pages = [(await _get(client, url))[1] for url in ALL_GET_PAGES]
        _, confirm = await _post(client, MODE_URL, {"mode": "on"}, stamp=await _stamp(factory))
        posts = {u for m, u in _settings_routes(client) if m == "POST"}
    assert posts == {_number_url("warn_days"), MODE_URL}
    for html in (*pages, confirm):
        assert _links(html) <= ALLOWED_LINKS, _links(html) - ALLOWED_LINKS
    # the waiting list is a sentence, with no control beside it
    assert _fold(texts.admin_life_batch_line.format(n="۰")) in _text_of(pages[1])


# --------------------------------------------------------------------------- #
# No secret on any settings page
# --------------------------------------------------------------------------- #

SECRET_FIELDS = {
    "BANK_SMS_TOKEN": "BLUSMS_TOKEN_PRIVATE-0123456789abcdef0123456789abcdef",
    "BANK_SMS_ACCOUNT_KEY": "BLUSMS_ACCOUNT_PRIVATE",
    "BANK_SMS_URL": "https://blusms.private-host.test/bank-sms/blu",
    "BOT_TOKEN": TOKEN,
    "WEBHOOK_SECRET": "WHSECRET-7f3a9c",
    "DATABASE_URL": "postgresql+asyncpg://dbuser:DBPASS-91c2@db.internal-host.test/prod",
    "PASARGUARD_BASE_URL": "https://panelUser:PANELPASS-52b7@panel.hidden-host.test:8443/",
    "PASARGUARD_API_KEY": "pg_key_ENVAPIKEY-3d8e",
    "PASARGUARD_SUB_PATH": "subpath-SECRET-44a",
    "PASARGUARD_DB_URL": "mysql://root:PGDBPASS-17dd@127.0.0.1/pg",
    "PAYMENT_CARD_NUMBER": "6037991234567890",
    "PAYMENT_CARD_HOLDER_NAME": "HOLDERNAME-QQ",
    "SENTRY_DSN": "https://SENTRYKEY-aa11@sentry.hidden-host.test/42",
}
DB_SECRETS = {
    "base_url": "https://dbadmin:DBROWPASS-88e1@dbpanel.hidden-host.test",
    "admin_username": "dbadmin-user-ZZ",
    "admin_password": "ADMINPASS-66f0",
    "api_key": "pg_key_DBROWKEY-71c4",
    "sub_path": "dbsub-SECRET-55b9",
}
NEEDLES = [
    "BLUSMS_TOKEN_PRIVATE",
    "BLUSMS_ACCOUNT_PRIVATE",
    "blusms.private-host.test",
    "TEST-PANEL-TOKEN",
    "WHSECRET",
    "DBPASS",
    "PANELPASS",
    "panelUser",
    "hidden-host",
    "ENVAPIKEY",
    "SECRET-44a",
    "PGDBPASS",
    "6037991234567890",
    "HOLDERNAME",
    "SENTRYKEY",
    "DBROWPASS",
    "dbadmin",
    "ADMINPASS",
    "DBROWKEY",
    "SECRET-55b9",
    "pg_key_",
]


def test_every_secret_looking_setting_is_seeded_in_the_no_secret_test() -> None:
    """A new secret-like field in ``Settings`` must be added above, or this fails."""
    from bot.config import Settings

    words = ("secret", "token", "password", "key", "url", "dsn", "card")
    secretish = {
        field.alias
        for name, field in Settings.model_fields.items()
        if any(w in name for w in words)
    }
    public = {"WEB_PANEL_URL"}  # the panel's own public address
    assert secretish - public <= set(SECRET_FIELDS), secretish - public - set(SECRET_FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_no_settings_page_renders_a_secret(shop, owner: bool) -> None:
    async with shop() as s:
        s.add(PasarGuardSettings(id=1, timeout=12.5, **DB_SECRETS))
        # a watcher row for the panel, and the group's title, so those pages have data too
        s.add(PanelNodeState(node_id=PANEL_NODE_ID, name="panel", status="connected"))
        s.add(
            ReportSettings(
                id=1, chat_id=-1001, chat_title="گروه من", topics={}, verified={}, enabled=1
            )
        )
        s.add(BrandAsset(key=QR_BACKGROUND_KEY, content=b"\x89PNG-FAKE-IMAGE-BYTES"))
        await s.commit()
    configured = panel_settings(
        **SECRET_FIELDS,
        **({"STORE_OWNER_TELEGRAM_IDS": str(ENV_ADMIN)} if owner else {}),
    )
    async with panel_client(shop, configured) as client:
        for url in ALL_GET_PAGES:
            status, html = await _get(client, url)
            assert status == 200, url
            for needle in NEEDLES:
                assert needle not in html, f"{needle!r} is on {url}"
        # and no error page, refusal or confirm page carries one either
        for _status, html in (
            await _post(client, MODE_URL, {"mode": "on"}, stamp="x"),
            await _post(
                client, _number_url("warn_days"), {"value": "zz"}, stamp=await _stamp(shop)
            ),
        ):
            for needle in NEEDLES:
                assert needle not in html


@pytest.mark.asyncio
async def test_the_panel_page_says_only_whether_the_connection_is_set(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    async with panel_client(factory, panel_settings(PASARGUARD_API_KEY="")) as client:
        _, off = await _get(client, "/panel/settings/panel")
    async with panel_client(
        factory, panel_settings(PASARGUARD_API_KEY="pg_key_ENVAPIKEY-3d8e")
    ) as client:
        _, on = await _get(client, "/panel/settings/panel")
    assert "تنظیم نشده" in off and "تنظیم شده" not in off.replace("تنظیم نشده", "")
    assert "تنظیم شده" in on


# --------------------------------------------------------------------------- #
# The pages' figures are what the services return
# --------------------------------------------------------------------------- #


async def _seed_everything(factory: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    await _seed(
        factory,
        mode=DELETE_MODE_ON,
        rehearsal=now - timedelta(days=2),
        warn_days=(7, 2),
        percents=(30, 10, 2),
    )
    async with factory() as s:
        s.add(
            ReportSettings(
                id=1,
                chat_id=-1002233,
                chat_title="گزارش‌های <b>نئو</b>",
                topics={TOPIC_KEYS[0]: 11, TOPIC_KEYS[1]: 12},
                verified={TOPIC_KEYS[0]: now.isoformat()},
                enabled=1,
            )
        )
        s.add(BrandAsset(key=QR_BACKGROUND_KEY, content=b"img"))
        s.add(
            PanelNodeState(
                node_id=PANEL_NODE_ID,
                name="panel",
                status="unreachable",
                status_since=now - timedelta(hours=3),
                last_checked_at=now - timedelta(minutes=2),
                checks_in_status=5,
            )
        )
        s.add(
            PanelNodeState(
                node_id=7,
                name="سرور-هلند",
                status="error",
                status_since=now - timedelta(hours=1),
                last_checked_at=now,
                checks_in_status=3,
            )
        )
        s.add(LifecycleBatch(kind="stop", status="open", numbers_key="k"))
        await s.commit()


@pytest.mark.asyncio
async def test_the_lifecycle_page_shows_the_figures_the_bots_screen_shows(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await _seed_everything(factory)
    async with factory() as s:
        config = await load_config(s, seed_days=SEED_DAYS)
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _get(client, LIFECYCLE)
    folded = _text_of(html)
    for field, label in FIELD_LABELS:
        assert f"{label} {current_value_text(field, config)}" in folded, field
    # the bot's whole status block, line by line (label and value both on the page)
    _has_figures(html, screen_text(config))
    assert texts.admin_life_mode_names[DELETE_MODE_ON] in folded
    assert texts.admin_life_note_on.strip().split("\n")[0][:30] in folded
    from services.ux_format import format_jalali

    assert format_jalali(config.last_rehearsal_at, persian_digits=True) in folded
    # every prefilled box starts from the stored value, in Persian digits
    assert 'value="۷ ۲"' in html and 'value="۳۰ ۱۰ ۲"' in html


@pytest.mark.asyncio
async def test_each_lifecycle_box_has_a_short_label_a_hint_and_the_current_value(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await _seed_everything(factory)
    async with factory() as s:
        config = await load_config(s, seed_days=SEED_DAYS)
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _get(client, LIFECYCLE)
    folded = _text_of(html)
    for field, _label in FIELD_LABELS:
        label, hint = texts.web_life_fields[field]
        assert label in folded
        assert hint.format(max_days="۳۶۵", max_percent="۹۹") in folded
        assert f"الان: {current_value_text(field, config)}" in folded
        assert texts.admin_life_prompts[field].split("\n")[1] not in folded, "the bot's question"
    assert texts.web_life_fields["warn_days"][0] == "چند روز مانده به انقضا"
    assert str(MAX_DAYS) in "365" and "تست رایگان هشدار ندارد" in folded
    assert "—" not in folded.split("عددها")[1].split("حذف خودکار")[0]


@pytest.mark.asyncio
async def test_the_report_group_page_matches_the_readiness_the_bot_prints(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await _seed_everything(factory)
    async with factory() as s:
        readiness = await get_readiness(s, create=False)
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _get(client, "/panel/settings/reports")
    folded = _text_of(html)
    assert readiness.attached and readiness.ready_count == 1
    assert f"{readiness.ready_count}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) in folded
    assert texts.admin_reports_state_on in folded
    for topic in readiness.topics:
        assert topic.title_fa in folded
    # the title is user input: escaped, never markup
    assert "&lt;b&gt;نئو&lt;/b&gt;" in html and "<b>نئو</b>" not in html
    marks = re.findall(r"<li><span>[^<]*</span><span>([^<]*)</span></li>", html)
    assert sorted(marks) == sorted(["✅", "🟡", *["⚪️"] * (len(readiness.topics) - 2)])


@pytest.mark.asyncio
async def test_the_report_group_page_for_a_shop_with_no_group_says_so(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _get(client, "/panel/settings/reports")
    assert texts.admin_reports_not_attached in html


@pytest.mark.asyncio
async def test_the_panel_page_shows_the_watchers_stored_state(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    await _seed_everything(factory)
    async with panel_client(
        factory, _owner_settings(PASARGUARD_API_KEY="pg_key_x", PANEL_WATCH_INTERVAL_SECONDS=120)
    ) as client:
        _, html = await _get(client, "/panel/settings/panel")
    folded = _text_of(html)
    assert "پنل در دسترس نیست" in folded  # row 0 is down for 5 checks
    assert "سرور-هلند" in folded and "قطع است" in folded
    assert "تنظیم شده" in folded


@pytest.mark.asyncio
async def test_the_panel_page_with_the_watch_off_says_the_watch_is_off(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    async with panel_client(factory, _owner_settings(PANEL_WATCH_INTERVAL_SECONDS=0)) as client:
        _, html = await _get(client, "/panel/settings/panel")
    assert "پایش خودکار پنل خاموش است" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [True, False])
async def test_the_qr_page_says_which_background_is_in_use(
    two_sessions: Factory, custom: bool
) -> None:
    factory, _ = two_sessions
    if custom:
        async with factory() as s:
            s.add(BrandAsset(key=QR_BACKGROUND_KEY, content=b"img"))
            await s.commit()
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _get(client, "/panel/settings/qr")
    mine, template = texts.admin_qr_bg_current_custom, texts.admin_qr_bg_current_template
    assert (mine in html) is custom and (template in html) is (not custom)


@pytest.mark.asyncio
async def test_the_home_lists_the_four_sections_with_the_services_lines(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await _seed_everything(factory)
    settings = _owner_settings(PASARGUARD_API_KEY="pg_key_x")
    async with factory() as s:
        cards = await overview(s, settings, base_url="/panel/settings", now=datetime.now(UTC))
    async with panel_client(factory, settings) as client:
        _, html = await _get(client, "/panel/settings")
    folded = _text_of(html)
    assert [c.url for c in cards] == [
        "/panel/settings/panel",
        "/panel/settings/reports",
        "/panel/settings/qr",
        "/panel/settings/lifecycle",
    ]
    for card in cards:
        assert card.title in folded and card.line in folded, card
        assert f'hx-get="{card.url}"' in html
    assert "بازاریابی" not in html and "marketing" not in html.lower()


@pytest.mark.asyncio
async def test_the_stamp_on_the_page_is_the_stamp_a_write_must_carry(shop) -> None:
    async with panel_client(shop, _owner_settings()) as client:
        _, html = await _get(client, LIFECYCLE)
        stamp = re.search(r'name="stamp" value="([^"]+)"', html)
        assert stamp is not None
        status, _ = await _post(
            client, _number_url("warn_days"), {"value": "4"}, stamp=stamp.group(1)
        )
        assert status == 200
        _, html_after = await _get(client, LIFECYCLE)
    assert re.search(r'name="stamp" value="([^"]+)"', html_after).group(1) != stamp.group(1)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_looking_after_a_save_shows_the_saved_value_and_the_bots_saved_line(shop) -> None:
    async with panel_client(shop, _owner_settings()) as client:
        status, html = await _post(
            client,
            _number_url("delete_after_expiry_days"),
            {"value": "14"},
            stamp=await _stamp(shop),
        )
    assert status == 200
    folded = _text_of(html)
    assert texts.admin_life_saved in folded
    assert texts.admin_life_rehearsal_voided in folded
    assert "۱۴ روز" in folded
    # the rehearsal the old number was rehearsed with is gone
    assert (await _row(shop))["last_rehearsal_at"] is None  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_user_with_html_in_a_node_name_is_escaped(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with factory() as s:
        s.add(
            PanelNodeState(
                node_id=9, name="<script>alert(1)</script>", status="error", checks_in_status=4
            )
        )
        await s.commit()
    async with panel_client(factory, _owner_settings(PANEL_WATCH_INTERVAL_SECONDS=120)) as client:
        _, html = await _get(client, "/panel/settings/panel")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


@pytest.mark.asyncio
async def test_a_banned_admin_is_refused_on_the_settings_pages(shop) -> None:
    await add_user(shop, 7777, is_admin=True, is_banned=True)
    async with panel_client(shop, panel_settings(ADMIN_TELEGRAM_IDS="7777")) as client:
        for url in ALL_GET_PAGES:
            assert (await client.get(url, headers={INIT_HEADER: init_data(7777)})).status == 403
