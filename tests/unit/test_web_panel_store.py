"""«فروشگاه» in the Mini App: the bot's store screens plus one write, a plan's price.

What these defend: the page shows what the services return (the bot's own status
lines and warnings), a price edit goes through the same call the bot's prompt
ends in and leaves the same change-log entry, a stale form is refused and never
merged, a double-submitted form applies once, and the control is not there for
an admin who may not use it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
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
from bot.handlers import store_admin as bot_store
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models.store import StoreVersion
from services.store_admin import (
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    update_draft_category,
    update_draft_package,
)
from services.store_dashboard import find_group, group_facts, plan_entries, retail_cell
from services.store_edit import (
    PACKAGE_EDIT_NOTE,
    TEXT_PLAN_MISSING,
    TEXT_STALE,
    published_store_version,
)
from services.store_strategy import list_store_changes
from services.ux_format import fa_toman
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


def _owner_settings() -> object:
    return panel_settings(STORE_OWNER_TELEGRAM_IDS=str(ENV_ADMIN))


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    """حجمی → ویژه (panel 11, two plans) and اقتصادی (panel 22); نامحدود with no panel group."""
    async with factory() as s:
        top = await create_draft_category(s, title_fa="حجمی", kind="volume")
        vip = await create_draft_category(s, title_fa="ویژه", kind="volume", parent_id=top.id)
        eco = await create_draft_category(s, title_fa="اقتصادی", kind="volume", parent_id=top.id)
        unl = await create_draft_category(s, title_fa="نامحدود", kind="unlimited")
        await update_draft_category(s, vip.id, panel_group_ids="11")
        await update_draft_category(s, eco.id, panel_group_ids="22")
        for gb, days, price in ((50, 30, 150_000), (100, 60, 270_000)):
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


async def _slugs(factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    async with factory() as s:
        view = await get_catalog_view(s)
    groups = {c.title_fa: c.slug for c in view.categories}
    plans = {p.title_fa: p.slug for p in view.packages}
    return SimpleNamespace(groups=groups, plans=plans)


async def _price(factory: async_sessionmaker[AsyncSession], plan_slug: str) -> int | None:
    async with factory() as s:
        view = await get_catalog_view(s)
        pkg = next(p for p in view.packages if p.slug == plan_slug)
        return retail_cell(list(view.prices), pkg)


async def _log(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as s:
        return [e.description for e in await list_store_changes(s)]


async def _version_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(StoreVersion)) or 0)


def _form_version(html: str) -> str:
    found = re.search(r'name="version" value="(\d+)"', html)
    assert found, "the page carries the store version in every price form"
    return found.group(1)


async def _page(client: TestClient, group_slug: str) -> str:
    resp = await client.get(f"/panel/store/g/{group_slug}", headers=OWNER)
    assert resp.status == 200
    return await resp.text()


async def _post_price(
    client: TestClient, group: str, plan: str, version: str, price: str
) -> tuple[int, str]:
    resp = await client.post(
        f"/panel/store/g/{group}/plans/{plan}/price",
        data={"version": version, "price": price},
        headers=OWNER,
    )
    return resp.status, await resp.text()


@pytest_asyncio.fixture
async def seeded(
    two_sessions: Factory,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], SimpleNamespace]]:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory, await _slugs(factory)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_store_pages_need_the_panel_login(seeded) -> None:
    factory, slugs = seeded
    plan = slugs.plans["50 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        for path in ("/panel/store", f"/panel/store/g/{slugs.groups['ویژه']}"):
            assert (await client.get(path)).status == 401
            stranger = {INIT_HEADER: init_data(STRANGER)}
            assert (await client.get(path, headers=stranger)).status == 403
        url = f"/panel/store/g/{slugs.groups['ویژه']}/plans/{plan}/price"
        assert (await client.post(url, data={"version": "1", "price": "1"})).status == 401
    assert await _price(factory, plan) == 150_000


@pytest.mark.asyncio
async def test_the_dashboard_shows_the_lines_and_prices_the_services_return(seeded) -> None:
    factory, slugs = seeded
    async with factory() as s:
        view = await get_catalog_view(s)
    cat = find_group(view, slugs.groups["ویژه"])
    assert cat is not None
    facts = group_facts(view, cat)
    plans = plan_entries(view, cat)
    assert [p.price_toman for p in plans] == [150_000, 270_000], "the seed has two priced plans"

    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, slugs.groups["ویژه"])

    for line in facts.lines:
        assert line in html
    assert (
        len(facts.warnings) == 1
    ), "the 60-day plan has no 60-day tab: the bot warns, so does the page"
    for warning in facts.warnings:
        assert warning in html
    for plan in plans:
        assert plan.dims and plan.dims in html
        assert f'value="{fa_toman(plan.price_toman)}"' in html
        assert plan.slug in html
    # the groups column: the child sits under its parent, a group with no panel group is marked
    assert "<small>حجمی</small>ویژه" in html
    assert "<small>حجمی</small>اقتصادی" in html
    assert "⚠️ نامحدود" in html
    assert "هر تغییری که تأیید کنی همون لحظه برای مشتری فعال می‌شه." in html


@pytest.mark.asyncio
async def test_the_dashboard_is_the_bots_dashboard(seeded) -> None:
    """Every status and ⚠️ line the bot's group screen prints is on the page."""
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        for title in ("ویژه", "اقتصادی", "نامحدود", "حجمی"):
            html = await _page(client, slugs.groups[title])
            async with factory() as s:
                view = await get_catalog_view(s)
            cat = find_group(view, slugs.groups[title])
            assert cat is not None
            bot_lines = bot_store._dashboard_text(view, cat).split("\n")
            assert bot_lines[0] == texts.admin_store_dash_title.format(title=title)
            for line in bot_lines[1:]:
                if line:
                    # The page keeps the full no-panel-group sentence for its alert only.
                    shown = line.replace(
                        texts.admin_store_cat_groups_unset, texts.web_store_cat_groups_unset
                    )
                    assert shown in html, f"{title}: the bot says «{line}», the page does not"


@pytest.mark.asyncio
async def test_a_group_with_no_panel_group_says_it_is_not_for_sale(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, slugs.groups["نامحدود"])
    assert texts.admin_store_dash_warn_no_panel in html
    # Once: the facts line is the short twin, the alert carries the sentence.
    assert html.count(texts.admin_store_dash_warn_no_panel) == 1
    assert f"گروه پنل: {texts.web_store_cat_groups_unset}" in html
    assert texts.admin_store_cat_groups_unset not in html
    assert "—" not in html.split('<ul class="facts">')[1].split("</ul>")[0]


@pytest.mark.asyncio
async def test_the_home_opens_on_the_first_group_and_an_unknown_group_is_a_404(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        resp = await client.get("/panel/store", headers=OWNER)
        assert resp.status == 200
        assert "پلن‌های ویژه" in await resp.text()
        gone = await client.get("/panel/store/g/no-such-group", headers=OWNER)
        assert gone.status == 404
        assert "گروه محصول یافت نشد." in await gone.text()


@pytest.mark.asyncio
async def test_looking_writes_nothing_and_the_write_path_has_no_get(seeded) -> None:
    factory, slugs = seeded
    before = await _version_rows(factory)
    log_before = await _log(factory)
    plan = slugs.plans["50 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        await client.get("/panel/store", headers=OWNER)
        for title in ("ویژه", "نامحدود", "حجمی"):
            await _page(client, slugs.groups[title])
        url = f"/panel/store/g/{slugs.groups['ویژه']}/plans/{plan}/price?version=1&price=999"
        assert (await client.get(url, headers=OWNER)).status == 405
    assert await _version_rows(factory) == before, "a browse screen must not mint a draft"
    assert await _log(factory) == log_before
    assert await _price(factory, plan) == 150_000


@pytest.mark.asyncio
async def test_the_admin_nav_lists_the_store(two_sessions: Factory) -> None:
    """The nav is rendered per role by /panel/me/home, not by the public shell."""
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        shell = await (await client.get("/panel/")).text()
        html = await (await client.get("/panel/me/home", headers=OWNER)).text()
    assert "/panel/store" not in shell, "the public shell names no admin screen"
    assert html.count('hx-get="/panel/store"') >= 2, "side column and tab bar"
    assert "فروشگاه" in html


@pytest.mark.asyncio
async def test_the_first_screen_an_admin_sees_is_the_store(two_sessions: Factory) -> None:
    """Home's one `load` request asks for the store, and the store link is the marked one."""
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        html = await (await client.get("/panel/me/home", headers=OWNER)).text()
    loader = re.search(r'<div [^>]*hx-trigger="load"[^>]*>', html)
    assert loader is not None
    assert 'hx-get="/panel/store"' in loader.group(0)
    assert "/panel/finance" not in loader.group(0)
    links = re.findall(r"<a [^>]*data-nav[^>]*>", html)
    marked = [link for link in links if "aria-current" in link]
    assert marked and all('hx-get="/panel/store"' in link for link in marked)
    assert len(marked) == 2, "side column and tab bar"


# --------------------------------------------------------------------------- #
# The price edit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_price_edit_changes_the_price_and_the_log_says_so(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    other = slugs.plans["100 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        status, html = await _post_price(client, group, plan, version, "۱۶۰٬۰۰۰")
    assert status == 200
    assert texts.admin_store_saved in html
    assert f'value="{fa_toman(160_000)}"' in html, "the page comes back with the new price"
    assert await _price(factory, plan) == 160_000
    assert await _price(factory, other) == 270_000, "no other plan moves"
    # The log is read back to the owner, so the note is Persian, not an internal English one.
    assert (await _log(factory))[0] == "پلن ویرایش شد" == PACKAGE_EDIT_NOTE


@pytest.mark.asyncio
async def test_the_web_edit_and_the_bots_edit_leave_the_same_state(
    two_sessions: Factory, tmp_path: Path
) -> None:
    web_factory, _ = two_sessions
    await _seed(web_factory)
    web_slugs = await _slugs(web_factory)

    bot_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with bot_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot_factory = async_sessionmaker(bot_engine, expire_on_commit=False)
    try:
        await _seed(bot_factory)
        async with bot_factory() as s:
            view = await get_catalog_view(s)
            pkg = next(p for p in view.packages if p.title_fa == "50 گیگ")
            state = FSMContext(
                storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=ENV_ADMIN)
            )
            await state.set_state(AdminStates.store_waiting_pkg_price)
            await state.update_data(store_pkg_id=pkg.id)
            typed = SimpleNamespace(
                text="۱۶۰٬۰۰۰",
                from_user=SimpleNamespace(id=ENV_ADMIN),
                answer=AsyncMock(),
            )
            settings = SimpleNamespace(
                admin_telegram_ids=[ENV_ADMIN], store_owner_telegram_ids=[ENV_ADMIN]
            )
            await bot_store.store_pkg_fsm_input(typed, state, settings, None, s)  # type: ignore[arg-type]

        async with panel_client(web_factory, _owner_settings()) as client:
            group = web_slugs.groups["ویژه"]
            version = _form_version(await _page(client, group))
            status, _ = await _post_price(
                client, group, web_slugs.plans["50 گیگ"], version, "۱۶۰٬۰۰۰"
            )
        assert status == 200

        async def snapshot(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
            async with factory() as s:
                view = await get_catalog_view(s)
                return {
                    "prices": sorted(
                        (p.title_fa, retail_cell(list(view.prices), p)) for p in view.packages
                    ),
                    "log": [e.description for e in await list_store_changes(s)],
                    "version": await published_store_version(s),
                }

        assert await snapshot(web_factory) == await snapshot(bot_factory)
    finally:
        await bot_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "۰", "0", "-5", "12.5", "", "   "])
async def test_an_invalid_price_is_refused_with_the_bots_line_and_writes_nothing(
    seeded, bad: str
) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    rows, log = await _version_rows(factory), await _log(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        status, html = await _post_price(client, group, plan, version, bad)
    assert status == 400
    assert texts.admin_store_invalid_input in html
    assert await _price(factory, plan) == 150_000
    assert await _version_rows(factory) == rows
    assert await _log(factory) == log


# --------------------------------------------------------------------------- #
# Stale forms
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_form_from_before_another_edit_is_refused_and_nothing_is_merged(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        # the bot (or another tab) edits the same catalog meanwhile
        async with factory() as s:
            view = await get_catalog_view(s)
            pkg = next(p for p in view.packages if p.slug == slugs.plans["100 گیگ"])
            await update_draft_package(s, pkg.id, fixed_price_toman=280_000)
            await commit_and_activate(s, notes="پلن ویرایش شد")
        log_before = await _log(factory)
        status, html = await _post_price(client, group, plan, version, "999٬000")
    assert status == 409
    assert TEXT_STALE in html
    assert f'hx-get="/panel/store/g/{group}"' in html, "a way to reload the screen"
    assert await _price(factory, plan) == 150_000, "the pending edit was not applied"
    assert await _price(factory, slugs.plans["100 گیگ"]) == 280_000, "the other edit stands"
    assert await _log(factory) == log_before


@pytest.mark.asyncio
async def test_the_same_form_sent_twice_changes_the_price_once(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        log_before = len(await _log(factory))
        first, _ = await _post_price(client, group, plan, version, "160000")
        second, html = await _post_price(client, group, plan, version, "170000")
    assert (first, second) == (200, 409)
    assert TEXT_STALE in html
    assert await _price(factory, plan) == 160_000
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
async def test_two_forms_sent_at_the_same_moment_apply_once(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        log_before = len(await _log(factory))
        results = await asyncio.gather(
            _post_price(client, group, plan, version, "160000"),
            _post_price(client, group, plan, version, "170000"),
        )
    assert sorted(status for status, _ in results) == [200, 409]
    assert await _price(factory, plan) in (160_000, 170_000)
    assert len(await _log(factory)) == log_before + 1


# --------------------------------------------------------------------------- #
# Slug is the identity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_row_id_from_an_older_version_edits_nothing(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    other = slugs.plans["100 گیگ"]
    async with factory() as s:
        view = await get_catalog_view(s)
        old_ids = {p.slug: str(p.id) for p in view.packages}
    async with panel_client(factory, _owner_settings()) as client:
        version = _form_version(await _page(client, group))
        assert (await _post_price(client, group, plan, version, "155000"))[0] == 200
        version = _form_version(await _page(client, group))  # every publish minted new row ids
        async with factory() as s:
            view = await get_catalog_view(s)
            assert {str(p.id) for p in view.packages}.isdisjoint(old_ids.values())
        before = await _log(factory)
        for stale_id in old_ids.values():
            status, html = await _post_price(client, group, stale_id, version, "1000")
            assert status == 400 and TEXT_PLAN_MISSING in html
        assert await _log(factory) == before
        assert await _price(factory, plan) == 155_000
        assert await _price(factory, other) == 270_000
        # the slug still finds its plan after the ids changed
        assert (await _post_price(client, group, other, version, "275000"))[0] == 200
    assert await _price(factory, other) == 275_000
    assert await _price(factory, plan) == 155_000


# --------------------------------------------------------------------------- #
# Who may write
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_reads_but_cannot_write(seeded) -> None:
    factory, slugs = seeded
    group, plan = slugs.groups["ویژه"], slugs.plans["50 گیگ"]
    async with panel_client(factory, panel_settings()) as client:  # admin, no store owner
        html = await _page(client, group)
        assert 'name="price"' not in html and 'class="price-form"' not in html
        assert 'name="version"' not in html
        assert texts.admin_store_hub_readonly in html
        assert fa_toman(150_000) in html, "the price is still readable"
        status, refusal = await _post_price(client, group, plan, "1", "160000")
    assert status == 403
    assert texts.admin_store_owner_only in refusal
    assert await _price(factory, plan) == 150_000


@pytest.mark.asyncio
async def test_the_owner_sees_the_edit_control_only_on_a_ready_plan(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, slugs.groups["ویژه"])
    assert html.count('class="price-form"') == 2
    assert texts.admin_store_hub_readonly not in html


@pytest.mark.asyncio
async def test_the_store_page_of_a_group_holding_a_plan_with_no_devices_has_no_dash(
    seeded,
) -> None:
    """A plan whose devices is empty shows an empty cell, not «—» (Piece A2)."""
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, slugs.groups["نامحدود"])
    # The label span stays
    assert '<span class="cl">کاربر همزمان</span>' in html
    # The cell must not show a dash
    plans_block = html.split('<div class="plans">')[1].split("</section>")[0]
    assert "—" not in plans_block
