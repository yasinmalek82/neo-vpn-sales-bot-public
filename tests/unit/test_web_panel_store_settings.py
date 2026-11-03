"""A group's four settings sections on the Mini App (1b of the store step).

What these defend, each with a test that goes red when the real line is broken:

* a web write leaves the same state and the same «تغییرات فروشگاه» row the bot's
  control leaves for the same input (parity, per setting, against the bot's handler);
* a control shows on the page exactly when the bot's keyboard shows it;
* the stop-sale switches have no form and no route on the web;
* every POST under ``/panel/store`` obeys one guard (login, admin, owner, current
  store version, GET writes nothing, a double submit applies once), and a route
  added later with no case here fails the build;
* «نامحدود» devices stays ``0``; a destructive change asks first with the bot's
  count and removes nothing before the second request; a number past SQLite's
  64-bit integer is the invalid-input line, not a 500.
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
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import (
    StoreCB,
    store_cat_group_keyboard,
    store_cat_panel_keyboard,
    store_cat_sale_keyboard,
    store_cat_trial_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models.store import StoreVersion
from services.store_admin import (
    StoreAdminError,
    add_category_duration,
    commit_and_activate,
    count_prices_for_duration,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    set_category_premium_panel_groups,
    set_category_tiers_enabled,
    set_category_trial,
    update_draft_category,
)
from services.store_catalog import category_volume_hwid_limit
from services.store_dashboard import retail_cell
from services.store_edit import TEXT_STALE, published_store_version
from services.store_group_settings import (
    STOP_SALE_CONTROLS,
    edit_group_setting,
)
from services.store_strategy import list_store_changes
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


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    """حجمی → ویژه (panel 11, trial, premium 21 with tiers on, tabs 1+3 months, two plans) and اقتصادی;
    نامحدود (panel 33); ساده (a volume group with no plans and no children, panel 7)."""
    async with factory() as s:
        top = await create_draft_category(s, title_fa="حجمی", kind="volume")
        vip = await create_draft_category(s, title_fa="ویژه", kind="volume", parent_id=top.id)
        eco = await create_draft_category(s, title_fa="اقتصادی", kind="volume", parent_id=top.id)
        unl = await create_draft_category(s, title_fa="نامحدود", kind="unlimited")
        plain = await create_draft_category(s, title_fa="ساده", kind="volume")
        for cat, ids in ((vip, "11"), (eco, "22"), (unl, "33"), (plain, "7")):
            await update_draft_category(s, cat.id, panel_group_ids=ids)
        await set_category_trial(s, vip.id, data_limit_gb=1, duration_days=2)
        await set_category_premium_panel_groups(s, vip.id, [21])
        await set_category_tiers_enabled(s, vip.id, enabled=True)
        await add_category_duration(s, vip.id, months=3)
        for gb, days, price in ((50, 30, 150_000), (100, 90, 270_000)):
            await create_draft_package(
                s,
                category_id=vip.id,
                title_fa=f"{gb} گیگ",
                fixed_price_toman=price,
                volume_gb=gb,
                duration_days=days,
            )
        await create_draft_package(
            s,
            category_id=unl.id,
            title_fa="یک کاربره",
            fixed_price_toman=250_000,
            concurrent_users=1,
            duration_months=1,
        )
        await commit_and_activate(s, notes="seed")


async def _groups(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async with factory() as s:
        view = await get_catalog_view(s)
    return {c.title_fa: str(c.slug) for c in view.categories}


async def _log(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as s:
        return [e.description for e in await list_store_changes(s)]


async def _version(factory: async_sessionmaker[AsyncSession]) -> int | None:
    async with factory() as s:
        return await published_store_version(s)


async def _version_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(StoreVersion)) or 0)


async def _snapshot(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    """Everything a settings write can touch, in a form two databases can be compared by."""
    async with factory() as s:
        view = await get_catalog_view(s)
        return {
            "groups": sorted(
                (
                    str(c.slug),
                    str(c.title_fa),
                    bool(c.enabled),
                    int(c.sort_order),
                    list(c.panel_group_ids or []),
                    dict(c.sales_flags or {}),
                )
                for c in view.categories
            ),
            "plans": sorted(
                (str(p.slug), retail_cell(list(view.prices), p), dict(p.options or {}))
                for p in view.packages
            ),
            "log": [e.description for e in await list_store_changes(s)],
            "version": await published_store_version(s),
        }


async def _page(client: TestClient, group_slug: str, headers: dict[str, str] = OWNER) -> str:
    resp = await client.get(f"/panel/store/g/{group_slug}", headers=headers)
    assert resp.status == 200
    return await resp.text()


def _section(html: str, name: str) -> str:
    """The HTML of one settings section (from its id to the next section or the end)."""
    start = html.index(f'id="sec-{name}"')
    nxt = html.find('id="sec-', start + 1)
    return html[start : nxt if nxt != -1 else len(html)]


async def _post(
    client: TestClient,
    group_slug: str,
    tail: str,
    form: dict[str, str],
    *,
    version: int | None,
    headers: dict[str, str] = OWNER,
) -> tuple[int, str]:
    data = dict(form)
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(
        f"/panel/store/g/{group_slug}/settings/{tail}", data=data, headers=headers
    )
    return resp.status, await resp.text()


@pytest_asyncio.fixture
async def seeded(
    two_sessions: Factory,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], dict[str, str]]]:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory, await _groups(factory)


# --------------------------------------------------------------------------- #
# The settings: (group, URL tail, form) for the web, and the bot's own control
# --------------------------------------------------------------------------- #


def _bot_settings() -> Any:
    return panel_settings(STORE_OWNER_TELEGRAM_IDS=str(ENV_ADMIN))


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


BotDriver = Callable[[AsyncSession, int], Awaitable[None]]


def _tap(handler_name: str, prefix: str) -> BotDriver:
    async def drive(s: AsyncSession, cat_id: int) -> None:
        await getattr(ha, handler_name)(_callback(f"{prefix}{cat_id}"), _bot_settings(), None, s)

    return drive


def _typed_into(handler_name: str, state: Any, text: str, *, data_key: str) -> BotDriver:
    async def drive(s: AsyncSession, cat_id: int) -> None:
        fsm = _fsm()
        await fsm.set_state(state)
        await fsm.update_data(**{data_key: cat_id})
        await getattr(ha, handler_name)(_typed(text), fsm, _bot_settings(), None, s)

    return drive


def _typed_hwid(text: str) -> BotDriver:
    async def drive(s: AsyncSession, cat_id: int) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_new_pkg_users)
        await fsm.update_data(hwid_cat_id=cat_id, hwid_pkg_id=None)
        await ha.store_pkg_users_typed(_typed(text), fsm, s, _bot_settings(), None)

    return drive


def _typed_months(text: str) -> BotDriver:
    async def drive(s: AsyncSession, cat_id: int) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_cat_duration)
        await fsm.update_data(store_cat_id=cat_id)
        await ha.store_cat_dur_months(_typed(text), fsm, _bot_settings(), None, s)

    return drive


async def _bot_dur_none(s: AsyncSession, cat_id: int) -> None:
    await ha.store_cat_dur_none(
        _callback(f"{StoreCB.CAT_DUR_NONE}{cat_id}"), _bot_settings(), None, s, _fsm()
    )


async def _bot_dur_del(s: AsyncSession, cat_id: int) -> None:
    await ha.store_cat_dur_del_ok(
        _callback(f"{StoreCB.CAT_DUR_DEL_OK}{cat_id}:90"), _bot_settings(), None, s
    )


class Case:
    def __init__(
        self,
        group: str,
        tail: str,
        form: dict[str, str],
        bot: BotDriver,
        canonical: str,
    ) -> None:
        self.group, self.tail, self.form, self.bot, self.canonical = (
            group,
            tail,
            form,
            bot,
            canonical,
        )

    def __repr__(self) -> str:
        return f"{self.group}:{self.tail}"


_BASE = "/panel/store/g/{group_slug}/settings/"
_TOGGLE = _BASE + "toggle/{setting}"
CASES = [
    Case("ویژه", "toggle/renew", {}, _tap("store_cat_toggle_rn", StoreCB.CAT_RN), _TOGGLE),
    Case("ویژه", "toggle/flat", {}, _tap("store_cat_toggle_flat", StoreCB.CAT_FLAT), _TOGGLE),
    Case("ویژه", "toggle/tiers", {}, _tap("store_cat_toggle_tiers", StoreCB.CAT_TIERS), _TOGGLE),
    Case(
        "ویژه",
        "toggle/custom",
        {},
        _tap("store_cat_toggle_custom", StoreCB.CAT_CUSTOM),
        _TOGGLE,
    ),
    Case(
        "ویژه",
        "toggle/reseller-trial",
        {},
        _tap("store_cat_toggle_reseller_trial", StoreCB.CAT_RTRIAL),
        _TOGGLE,
    ),
    Case("ساده", "toggle/kind", {}, _tap("store_cat_toggle_kind", StoreCB.CAT_KIND), _TOGGLE),
    Case(
        "ویژه",
        "title",
        {"title": "ویژه ۲"},
        _typed_into(
            "store_cat_fsm_input",
            AdminStates.store_waiting_cat_title,
            "ویژه ۲",
            data_key="store_cat_id",
        ),
        _BASE + "title",
    ),
    Case(
        "ویژه",
        "sort",
        {"sort": "۴۵"},
        _typed_into(
            "store_cat_fsm_input", AdminStates.store_waiting_cat_sort, "۴۵", data_key="store_cat_id"
        ),
        _BASE + "sort",
    ),
    Case(
        "ویژه",
        "trial",
        {"gb": "۳", "days": "۷"},
        _typed_into(
            "store_cat_trial_input",
            AdminStates.store_waiting_cat_trial,
            "3 7",
            data_key="store_cat_id",
        ),
        _BASE + "trial",
    ),
    Case(
        "ویژه",
        "trial",
        {"gb": "0"},
        _typed_into(
            "store_cat_trial_input",
            AdminStates.store_waiting_cat_trial,
            "0",
            data_key="store_cat_id",
        ),
        _BASE + "trial",
    ),
    Case("ویژه", "hwid", {"hwid": "3"}, _typed_hwid("3"), _BASE + "hwid"),
    Case("ویژه", "hwid", {"hwid": "۰"}, _typed_hwid("۰"), _BASE + "hwid"),
    Case(
        "ویژه",
        "panel-groups",
        {"ids": "12, ۱۳"},
        _typed_into(
            "store_group_pick_typed",
            AdminStates.store_waiting_cat_groups,
            "12, ۱۳",
            data_key="store_cat_id",
        ),
        _BASE + "panel-groups",
    ),
    Case(
        "ویژه",
        "premium-groups",
        {"ids": "21,22"},
        _typed_into(
            "store_group_pick_typed",
            AdminStates.store_waiting_cat_tier_groups,
            "21,22",
            data_key="store_cat_id",
        ),
        _BASE + "premium-groups",
    ),
    Case("ویژه", "durations/add", {"months": "۶"}, _typed_months("۶"), _BASE + "durations/add"),
    Case(
        "ویژه",
        "durations/add",
        {"unlimited": "1"},
        _bot_dur_none,
        _BASE + "durations/add",
    ),
    Case(
        "ویژه",
        "durations/90/remove",
        {"confirm": "1"},
        _bot_dur_del,
        _BASE + "durations/{duration}/remove",
    ),
]


# The row «↩️ تغییرات» reads back to the owner, written out here and not imported: a
# note changed in services would otherwise change both paths and still agree.
EXPECTED_NOTE = {
    "toggle/renew": "تنظیمات گروه عوض شد",
    "toggle/flat": "فهرست ساده گروه عوض شد",
    "toggle/tiers": "تفکیک ویژه/اقتصادی عوض شد",
    "toggle/custom": "تنظیمات گروه عوض شد",
    "toggle/reseller-trial": "تنظیمات گروه عوض شد",
    "toggle/kind": "تنظیمات گروه عوض شد",
    "title": "گروه محصول ویرایش شد",
    "sort": "گروه محصول ویرایش شد",
    "trial": "تست رایگان گروه «ویژه» عوض شد",
    "hwid": "کاربر همزمان خرید سفارشی عوض شد",
    "panel-groups": "گروه پنل ویرایش شد",
    "premium-groups": "گروه‌های پنل ویژه ویرایش شد",
    "durations/add": ("مدت ۶ ماهه به «ویژه» اضافه شد", "مدت بدون انقضا به «ویژه» اضافه شد"),
    "durations/90/remove": "مدت ۳ ماهه از «ویژه» برداشته شد",
}


def _expected_note(case: Case) -> str:
    note = EXPECTED_NOTE[case.tail]
    if isinstance(note, tuple):
        return note[1] if case.form.get("unlimited") else note[0]
    return note


@pytest_asyncio.fixture
async def twin(
    two_sessions: Factory, tmp_path: Path
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]]:
    """The web's database and a second one for the bot's path, seeded the same."""
    web_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed(web_factory)
        await _seed(bot_factory)
        yield web_factory, bot_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_web_write_leaves_the_state_and_log_the_bots_control_leaves(
    twin, case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, bot_factory = twin
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    slugs = await _groups(web_factory)

    async with bot_factory() as s:
        view = await get_catalog_view(s)
        cat_id = int(next(c.id for c in view.categories if c.slug == slugs[case.group]))
        await case.bot(s, cat_id)

    before = await _snapshot(web_factory)
    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            slugs[case.group],
            case.tail,
            case.form,
            version=await _version(web_factory),
        )
    assert status == 200, html
    after = await _snapshot(web_factory)
    assert (
        after != before
    ), "the write must have changed something for the comparison to mean anything"
    assert after == await _snapshot(bot_factory)
    assert after["log"][0] == _expected_note(case)  # type: ignore[index]
    assert texts.admin_store_saved in html


# --------------------------------------------------------------------------- #
# After a save the same section comes back with the bot's saved line
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tail", "form", "section"),
    [
        ("toggle/renew", {}, "sale"),
        ("trial", {"gb": "2", "days": "3"}, "trial"),
        ("panel-groups", {"ids": "14"}, "panel"),
        ("sort", {"sort": "5"}, "group"),
    ],
)
async def test_the_same_section_redraws_with_the_saved_line(seeded, tail, form, section) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], tail, form, version=await _version(factory)
        )
    assert status == 200
    assert f"✅ {texts.admin_store_saved}" in _section(html, section)
    for other in {"sale", "trial", "panel", "group"} - {section}:
        assert f"✅ {texts.admin_store_saved}" not in _section(html, other)


@pytest.mark.asyncio
async def test_the_page_after_a_save_carries_the_new_version_in_every_section(seeded) -> None:
    """A redraw of one section with the old version would make the next save in another one stale."""
    factory, slugs = seeded
    old = await _version(factory)
    async with panel_client(factory, _owner_settings()) as client:
        _status, html = await _post(client, slugs["ویژه"], "toggle/renew", {}, version=old)
    new = await _version(factory)
    assert new != old
    assert set(re.findall(r'name="version" value="(\d+)"', html)) == {str(new)}


# --------------------------------------------------------------------------- #
# Hide until relevant: the page and the bot's keyboard drive off one function
# --------------------------------------------------------------------------- #

# control -> (marker in the page, the bot's callback prefix, which keyboard)
CONTROL_MARKERS = {
    "renew": ("/settings/toggle/renew", StoreCB.CAT_RN, "sale"),
    "flat": ("/settings/toggle/flat", StoreCB.CAT_FLAT, "sale"),
    "durations": ("/settings/durations/add", StoreCB.CAT_DURS, "sale"),
    "tiers": ("/settings/toggle/tiers", StoreCB.CAT_TIERS, "sale"),
    "custom": ("/settings/toggle/custom", StoreCB.CAT_CUSTOM, "sale"),
    "custom_hwid": ("/settings/hwid", StoreCB.CAT_HWID, "sale"),
    "trial": ("/settings/trial", StoreCB.CAT_TRIAL, "trial"),
    "reseller_trial": ("/settings/toggle/reseller-trial", StoreCB.CAT_RTRIAL, "trial"),
    "panel_groups": ("/settings/panel-groups", StoreCB.CAT_GROUPS, "panel"),
    "premium_groups": ("/settings/premium-groups", StoreCB.CAT_TIER_GROUPS, "panel"),
    "title": ("/settings/title", StoreCB.CAT_TITLE, "group"),
    "sort": ("/settings/sort", StoreCB.CAT_SORT, "group"),
    "kind": ("/settings/toggle/kind", StoreCB.CAT_KIND, "group"),
}


async def _arrange(factory: async_sessionmaker[AsyncSession], group: str, **changes: Any) -> None:
    """Put a group in a state: ``flat``, ``tiers``, ``custom``, ``trial_off``, ``premium_off``."""
    slugs = await _groups(factory)
    async with factory() as s:
        view = await get_catalog_view(s)
        cat = next(c for c in view.categories if c.slug == slugs[group])
        from services.store_admin import set_category_flat_plan_list

        if "flat" in changes:
            await set_category_flat_plan_list(s, cat.id, enabled=changes["flat"])
        if "tiers" in changes:
            await set_category_tiers_enabled(s, cat.id, enabled=changes["tiers"])
        if "custom" in changes:
            await update_draft_category(s, cat.id, custom_purchase_mode=changes["custom"])
        if changes.get("trial_off"):
            await set_category_trial(s, cat.id, data_limit_gb=0, duration_days=0)
        await commit_and_activate(s, notes="arrange")


HIDE_CASES = [
    ("ویژه", {}),
    ("ویژه", {"tiers": False}),
    ("ویژه", {"tiers": True, "flat": True}),
    ("ویژه", {"flat": True}),
    ("ویژه", {"custom": "off"}),
    ("ویژه", {"custom": "resellers"}),
    ("ویژه", {"trial_off": True}),
    ("اقتصادی", {}),
    ("حجمی", {}),
    ("ساده", {}),
    ("ساده", {"flat": True, "custom": "off"}),
    ("نامحدود", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("group", "changes"), HIDE_CASES, ids=str)
async def test_a_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it(
    seeded, group: str, changes: dict[str, Any]
) -> None:
    factory, _ = seeded
    await _arrange(factory, group, **changes)
    slugs = await _groups(factory)
    async with factory() as s:
        view = await get_catalog_view(s)
    cat = next(c for c in view.categories if c.slug == slugs[group])
    keyboards = {
        "sale": store_cat_sale_keyboard(cat),
        "trial": store_cat_trial_keyboard(cat),
        "panel": store_cat_panel_keyboard(cat),
        "group": store_cat_group_keyboard(
            cat,
            has_plans=bool(view.packages_of(cat.id)),
            has_children=bool(view.children_of(cat.id)),
        ),
    }
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, slugs[group])
    seen_any = False
    for control, (marker, prefix, which) in CONTROL_MARKERS.items():
        bot_shows = any(
            (b.callback_data or "").startswith(prefix)
            for row in keyboards[which].inline_keyboard
            for b in row
        )
        page_shows = marker in _section(html, which)
        assert page_shows == bot_shows, (group, changes, control, "bot" if bot_shows else "no bot")
        seen_any = seen_any or page_shows
    assert seen_any


def _cat(parent_id: int | None = None, **flags: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"kind": "volume", "allow_new_sale": True, "allow_renew": True}
    base.update(flags)
    return SimpleNamespace(id=5, enabled=True, parent_id=parent_id, sales_flags=base)


@pytest.mark.parametrize(
    ("control", "shown", "hidden"),
    [
        ("durations", _cat(flat_plan_list=False), _cat(flat_plan_list=True)),
        ("tiers", _cat(), _cat(flat_plan_list=True)),
        ("custom", _cat(), _cat(kind="unlimited")),
        ("custom_hwid", _cat(custom_purchase_mode="everyone"), _cat(custom_purchase_mode="off")),
        ("custom_hwid", _cat(custom_purchase_mode="resellers"), _cat(kind="unlimited")),
        ("reseller_trial", _cat(trial_gb=1, trial_days=1), _cat()),
        ("premium_groups", _cat(tiers_enabled=True), _cat()),
        ("premium_groups", _cat(tiers_enabled=True), _cat(tiers_enabled=True, flat_plan_list=True)),
    ],
)
def test_visible_controls_hides_a_control_that_would_do_nothing(control, shown, hidden) -> None:
    from services.store_group_settings import visible_controls

    assert control in visible_controls(shown)
    assert control not in visible_controls(hidden)


def test_visible_controls_kind_and_subgroup_follow_what_the_group_holds() -> None:
    from services.store_group_settings import visible_controls

    assert {"kind", "subgroup"} <= visible_controls(_cat())
    assert "kind" not in visible_controls(_cat(), has_plans=True)
    assert "kind" not in visible_controls(_cat(), has_children=True)
    assert "subgroup" not in visible_controls(_cat(), has_plans=True)
    assert "subgroup" not in visible_controls(_cat(parent_id=4))


@pytest.mark.asyncio
async def test_a_forged_post_for_a_control_the_group_does_not_show_is_refused(seeded) -> None:
    factory, slugs = seeded
    await _arrange(factory, "ویژه", flat=True)  # durations, tiers and premium groups disappear
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        for tail, form in (
            ("toggle/tiers", {}),
            ("durations/add", {"months": "6"}),
            ("durations/90/remove", {"confirm": "1"}),
            ("premium-groups", {"ids": "5"}),
        ):
            status, html = await _post(client, slugs["ویژه"], tail, form, version=version)
            assert status == 409 and TEXT_STALE in html, tail
        # a group with plans has no kind switch; a group with a parent no sub-group
        status, _ = await _post(client, slugs["ویژه"], "toggle/kind", {}, version=version)
        assert status == 409
    assert await _snapshot(factory) == before


# --------------------------------------------------------------------------- #
# The stop-sale switches stay in the bot
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_sale_switch_is_not_writable_from_the_web(seeded) -> None:
    factory, slugs = seeded
    group = slugs["ویژه"]
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, group)
        for needle in (
            "new-sale",
            "new_sale",
            "allow_new_sale",
            "toggle/enabled",
            "toggle/visible",
        ):
            assert needle not in html, needle
        controls = re.sub(r'<p class="hint">.*?</p>', "", _section(html, "sale"), flags=re.S)
        assert "فروش جدید" not in controls, "only the hint names it"
        assert "نمایش به مشتری" not in re.sub(r'<p class="hint">.*?</p>', "", html, flags=re.S)
        before = await _snapshot(factory)
        version = await _version(factory)
        for setting in (
            "new-sale",
            "new_sale",
            "allow_new_sale",
            "sale",
            "enabled",
            "visible",
            "stop-sale",
        ):
            status, _ = await _post(client, group, f"toggle/{setting}", {}, version=version)
            assert status == 404, setting
        paths = [r.resource.canonical for r in client.app.router.routes() if r.resource]
        assert not [p for p in paths if "sale" in p.lower() and "/settings" in p]
    assert await _snapshot(factory) == before
    # the state is shown, read-only, by the dashboard's own line
    assert texts.admin_store_dash_sale.format(state="روشن") in html


@pytest.mark.asyncio
async def test_the_guard_refuses_a_stop_sale_control_even_if_a_route_asked_for_one(seeded) -> None:
    factory, slugs = seeded
    assert {"visible", "new_sale"} == STOP_SALE_CONTROLS
    async with factory() as s:
        for control in STOP_SALE_CONTROLS:
            with pytest.raises(ValueError):
                await edit_group_setting(
                    s,
                    _owner_settings(),
                    ENV_ADMIN,
                    expected_version=await published_store_version(s),
                    group_slug=slugs["ویژه"],
                    control=control,
                    apply=AsyncMock(),
                )


# --------------------------------------------------------------------------- #
# One guard for every write route
# --------------------------------------------------------------------------- #


def _concrete(case: Case, slugs: dict[str, str]) -> str:
    return f"/panel/store/g/{slugs[case.group]}/settings/{case.tail}"


@pytest.mark.asyncio
async def test_every_post_route_under_the_store_has_a_guard_case(seeded) -> None:
    """A write route added later with no case in CASES (and no entry for the price) fails here."""
    from tests.unit.test_web_panel_store_plans import PLAN_CANONICALS

    factory, _ = seeded
    covered = (
        {case.canonical for case in CASES}
        | {"/panel/store/g/{group_slug}/plans/{plan_slug}/price"}
        | PLAN_CANONICALS
    )
    async with panel_client(factory, _owner_settings()) as client:
        posts = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "POST" and r.resource and r.resource.canonical.startswith("/panel/store")
        }
    assert posts, "the store has write routes"
    assert posts == covered, f"uncovered: {posts - covered}, unknown: {covered - posts}"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_settings_write_needs_login_admin_owner_and_the_current_version(
    seeded, case: Case
) -> None:
    factory, slugs = seeded
    url = _concrete(case, slugs)
    version = await _version(factory)
    form = {**case.form, "version": str(version)}
    before = await _snapshot(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        assert (await client.post(url, data=form)).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (await client.post(url, data=form, headers=stranger)).status == 403
        # GET never writes (and the route has no GET)
        assert (await client.get(url, headers=OWNER)).status == 405
        assert (await client.get(f"{url}?version={version}", headers=OWNER)).status == 405
        # a form drawn from an older version
        status, html = await _post_to(client, url, case.form, version=(version or 0) - 1)
        assert status == 409 and TEXT_STALE in html
        status, _ = await _post_to(client, url, case.form, version=None)
        assert status == 409, "no version at all is as stale as a wrong one"
    assert await _snapshot(factory) == before
    assert await _version_rows(factory) == rows

    # an admin who is not a store owner reads but cannot write
    async with panel_client(factory, panel_settings()) as client:
        status, html = await _post_to(client, url, case.form, version=version)
        assert status == 403 and texts.admin_store_owner_only in html
    assert await _snapshot(factory) == before


async def _post_to(
    client: TestClient, url: str, form: dict[str, str], *, version: int | None
) -> tuple[int, str]:
    data = dict(form)
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(url, data=data, headers=OWNER)
    return resp.status, await resp.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_settings_form_sent_twice_applies_once(seeded, case: Case) -> None:
    factory, slugs = seeded
    url = _concrete(case, slugs)
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        first, _ = await _post_to(client, url, case.form, version=version)
        after_first = await _snapshot(factory)
        second, html = await _post_to(client, url, case.form, version=version)
    assert (first, second) == (200, 409)
    assert TEXT_STALE in html
    assert await _snapshot(factory) == after_first, "the second form changed nothing"
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES[:6] + CASES[10:12], ids=repr)
async def test_two_settings_forms_at_the_same_moment_apply_once(seeded, case: Case) -> None:
    factory, slugs = seeded
    url = _concrete(case, slugs)
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        results = await asyncio.gather(
            _post_to(client, url, case.form, version=version),
            _post_to(client, url, case.form, version=version),
        )
    assert sorted(status for status, _ in results) == [200, 409]
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
async def test_looking_at_every_group_writes_nothing(seeded) -> None:
    factory, slugs = seeded
    rows, log = await _version_rows(factory), await _log(factory)
    async with panel_client(factory, _owner_settings()) as client:
        for slug in slugs.values():
            await _page(client, slug)
    assert await _version_rows(factory) == rows and await _log(factory) == log


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_sees_no_settings_at_all(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, panel_settings()) as client:
        html = await _page(client, slugs["ویژه"])
    assert 'id="sec-' not in html and "/settings/" not in html
    assert texts.admin_store_hub_readonly in html


@pytest.mark.asyncio
async def test_a_group_row_id_from_an_older_version_edits_nothing(seeded) -> None:
    factory, slugs = seeded
    async with factory() as s:
        view = await get_catalog_view(s)
        old_id = str(next(c.id for c in view.categories if c.title_fa == "ویژه"))
    async with panel_client(factory, _owner_settings()) as client:
        assert (
            await _post(client, slugs["ویژه"], "toggle/renew", {}, version=await _version(factory))
        )[0] == 200
        before = await _snapshot(factory)
        status, html = await _post(
            client, old_id, "toggle/renew", {}, version=await _version(factory)
        )
    assert status == 404 and "گروه محصول یافت نشد" in html
    assert await _snapshot(factory) == before


# --------------------------------------------------------------------------- #
# Particular settings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["0", "۰"])
async def test_unlimited_devices_stays_zero_through_a_web_edit(seeded, typed: str) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        await _post(client, slugs["ویژه"], "hwid", {"hwid": "5"}, version=await _version(factory))
        status, html = await _post(
            client, slugs["ویژه"], "hwid", {"hwid": typed}, version=await _version(factory)
        )
    assert status == 200
    async with factory() as s:
        view = await get_catalog_view(s)
    cat = next(c for c in view.categories if c.slug == slugs["ویژه"])
    assert (cat.sales_flags or {})["volume_hwid_limit"] == 0
    assert category_volume_hwid_limit(cat) == 0
    assert "کاربر همزمان خرید سفارشی: نامحدود" in html


@pytest.mark.asyncio
async def test_a_blank_device_limit_is_refused_not_read_as_one(seeded) -> None:
    factory, slugs = seeded
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], "hwid", {"hwid": ""}, version=await _version(factory)
        )
    assert status == 400 and texts.admin_store_invalid_input in html
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_removing_a_duration_asks_first_with_the_bots_count_and_removes_nothing(
    seeded,
) -> None:
    factory, slugs = seeded
    group = slugs["ویژه"]
    async with factory() as s:
        view = await get_catalog_view(s)
        cat_id = int(next(c.id for c in view.categories if c.slug == group))
        bot_count = await count_prices_for_duration(s, cat_id, 90)
        await s.rollback()
    assert bot_count == 1, "the seed prices one plan at three months"
    before = await _snapshot(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, group, "durations/90/remove", {}, version=await _version(factory)
        )
        assert status == 200
        asked = texts.admin_store_dur_del_confirm.format(
            label="۳ ماهه", title="ویژه", count=bot_count
        )
        assert asked in html.replace("&#39;", "'")
        assert 'name="confirm" value="1"' in html
        assert await _snapshot(factory) == before, "asking removed nothing and logged nothing"
        assert await _version_rows(factory) == rows, "asking did not even leave a draft"
        # the second request is the removal
        status, page = await _post(
            client,
            group,
            "durations/90/remove",
            {"confirm": "1"},
            version=await _version(factory),
        )
    assert status == 200
    after = await _snapshot(factory)
    assert after["log"][0] == "مدت ۳ ماهه از «ویژه» برداشته شد"  # type: ignore[index]
    async with factory() as s:
        view = await get_catalog_view(s)
    vip = next(c for c in view.categories if c.slug == group)
    assert (vip.sales_flags or {})["durations"] == [30]
    assert len(after["plans"]) == len(before["plans"])  # type: ignore[arg-type]
    assert retail_cell_of(view, "100 گیگ") == 270_000, "removing a tab keeps the price cells"
    assert "۳ ماهه" not in _section(page, "sale")


def retail_cell_of(view: Any, title: str) -> int | None:
    pkg = next(p for p in view.packages if p.title_fa == title)
    return retail_cell(list(view.prices), pkg)


@pytest.mark.asyncio
async def test_the_last_duration_is_not_removable(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        await _post(
            client,
            slugs["ویژه"],
            "durations/90/remove",
            {"confirm": "1"},
            version=await _version(factory),
        )
        before = await _snapshot(factory)
        status, html = await _post(
            client,
            slugs["ویژه"],
            "durations/30/remove",
            {"confirm": "1"},
            version=await _version(factory),
        )
    assert status == 400 and texts.admin_store_dur_last in html
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_a_group_left_with_no_panel_group_says_it_is_not_for_sale(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ساده"], "panel-groups", {"ids": ""}, version=await _version(factory)
        )
    assert status == 200
    assert texts.admin_store_dash_warn_no_panel in html
    assert texts.web_store_cat_groups_unset in html
    assert html.count(texts.admin_store_dash_warn_no_panel) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["abc", "0", "1,x", "²", "1 2 3 4 5 6 7 8 9"])
async def test_panel_group_ids_are_parsed_by_the_bots_parser(seeded, typed: str) -> None:
    factory, slugs = seeded
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], "panel-groups", {"ids": typed}, version=await _version(factory)
        )
    assert status == 400
    assert "فقط شناسهٔ عددی گروه‌ها رو بفرست" in html or "حداکثر" in html
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_clearing_premium_groups_while_tiers_are_on_says_tiers_went_off(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], "premium-groups", {"ids": ""}, version=await _version(factory)
        )
    assert status == 200 and texts.admin_store_tier_disabled_no_groups in html


async def _live_services(
    factory: async_sessionmaker[AsyncSession], plan_title: str, count: int
) -> None:
    """``count`` live services bought on the plan called ``plan_title``."""
    from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
    from database.models.plan import DataLimitResetStrategy

    async with factory() as s:
        view = await get_catalog_view(s)
        slug = str(next(p.slug for p in view.packages if p.title_fa == plan_title))
        user = User(
            telegram_id=500,
            username="u",
            first_name="U",
            language_code="fa",
            referral_code="R0000500",
            is_admin=False,
        )
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
        s.add_all([user, plan])
        await s.flush()
        for n in range(count):
            username = f"svc{n}"
            s.add(
                Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    status=OrderStatus.ACTIVE,
                    price_toman=0,
                    discount_toman=0,
                    force_new=True,
                    panel_username=username,
                    store_package_slug=slug,
                )
            )
            s.add(
                PasarGuardAccount(user_id=user.id, panel_user_id=1000 + n, panel_username=username)
            )
        await s.commit()


@pytest.mark.asyncio
async def test_a_panel_group_change_that_would_strand_services_is_refused_with_the_count(
    seeded,
) -> None:
    factory, slugs = seeded
    await _live_services(factory, "50 گیگ", 3)
    before = await _snapshot(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], "panel-groups", {"ids": "14"}, version=await _version(factory)
        )
    assert status == 400
    section = _section(html, "panel")
    assert "۳ سرویس فعال را روی گروه قبلی جا می‌گذارد" in section
    assert "فقط از ربات انجام می‌شود" in section
    assert await _snapshot(factory) == before, "refused means nothing was saved"
    assert await _version_rows(factory) == rows, "and not even a draft is left behind"


@pytest.mark.asyncio
async def test_a_panel_group_change_that_strands_nothing_is_saved(seeded) -> None:
    factory, slugs = seeded
    await _live_services(factory, "50 گیگ", 3)
    async with panel_client(factory, _owner_settings()) as client:
        # the same ids again: no change of target, nothing left behind
        same, _ = await _post(
            client, slugs["ویژه"], "panel-groups", {"ids": "11"}, version=await _version(factory)
        )
        # a group no service sits in
        other, html = await _post(
            client, slugs["ساده"], "panel-groups", {"ids": "14"}, version=await _version(factory)
        )
    assert (same, other) == (200, 200)
    assert texts.admin_store_saved in html
    async with factory() as s:
        view = await get_catalog_view(s)
    assert next(c for c in view.categories if c.slug == slugs["ساده"]).panel_group_ids == [14]


@pytest.mark.asyncio
async def test_emptying_a_group_that_has_services_is_not_a_strand(seeded) -> None:
    """An empty target takes the group off sale; services stay where they are (the bot's rule)."""
    factory, slugs = seeded
    await _live_services(factory, "50 گیگ", 2)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], "panel-groups", {"ids": ""}, version=await _version(factory)
        )
    assert status == 200 and texts.admin_store_dash_warn_no_panel in html


# --------------------------------------------------------------------------- #
# A number past SQLite's 64-bit integer is the invalid-input line, never a 500
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tail", "form", "section"),
    [
        ("hwid", {"hwid": NINES}, "sale"),
        ("trial", {"gb": NINES, "days": "1"}, "trial"),
        ("trial", {"gb": "1", "days": NINES}, "trial"),
        ("sort", {"sort": NINES}, "group"),
        ("sort", {"sort": "-" + NINES}, "group"),
    ],
)
async def test_a_huge_number_in_a_setting_is_the_invalid_input_line(
    seeded, tail: str, form: dict[str, str], section: str
) -> None:
    factory, slugs = seeded
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, slugs["ویژه"], tail, form, version=await _version(factory)
        )
    assert status == 400
    assert texts.admin_store_invalid_input in _section(html, section)
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_a_huge_price_on_the_web_is_the_invalid_input_line(seeded) -> None:
    factory, slugs = seeded
    async with factory() as s:
        view = await get_catalog_view(s)
        plan = next(p.slug for p in view.packages if p.title_fa == "50 گیگ")
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        resp = await client.post(
            f"/panel/store/g/{slugs['ویژه']}/plans/{plan}/price",
            data={"version": str(await _version(factory)), "price": NINES},
            headers=OWNER,
        )
        html = await resp.text()
    assert resp.status == 400 and texts.admin_store_invalid_input in html
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_a_huge_price_in_the_bots_prompt_is_the_invalid_input_message(seeded) -> None:
    factory, slugs = seeded
    async with factory() as s:
        view = await get_catalog_view(s)
        pkg = next(p for p in view.packages if p.title_fa == "50 گیگ")
        state = _fsm()
        await state.set_state(AdminStates.store_waiting_pkg_price)
        await state.update_data(store_pkg_id=pkg.id)
        typed = _typed(NINES)
        await ha.store_pkg_fsm_input(typed, state, _bot_settings(), None, s)  # type: ignore[arg-type]
    sent = [call.args[0] for call in typed.answer.await_args_list]
    assert any(texts.admin_store_invalid_input in text for text in sent), sent
    assert StoreAdminError  # the refusal is the service's, not an unhandled error
