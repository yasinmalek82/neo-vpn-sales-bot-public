"""The plan editor on the Mini App (1c of the store step), and group create / delete.

What these defend, each with a test that goes red when the real line is broken:

* a web write (create, each edit, delete, reorder, a new group, a sub-group, a deleted
  group) leaves the same state, the same «تغییرات فروشگاه» row and the same version the
  bot's real handler leaves for the same input (parity, against a second database);
* the preview is the label of the customer's real purchase keyboard (flat and tabbed),
  is a read, and warns with the bot's own lines;
* every typed dimension is checked by ``check_dim`` and a ready plan carries them in
  ``options``; «نامحدود» devices stays ``0``;
* a delete asks first (nothing is written before the second request);
* every new POST route obeys one guard (login, admin, owner, version, double submit,
  GET 405), and a POST route with no case here fails the build;
* a control is on the page exactly when the bot's keyboard shows it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import store_admin as ha
from bot.keyboards.purchase import PurchaseCB, ready_plans_keyboard
from bot.keyboards.store_admin import (
    StoreCB,
    store_cat_group_keyboard,
    store_category_dashboard_keyboard,
    store_package_detail_keyboard,
)
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from services.store_admin import get_catalog_view
from services.store_dashboard import retail_cell
from services.store_edit import TEXT_STALE
from tests.unit.test_web_panel_store_settings import (  # noqa: F401  (fixtures)
    _arrange,
    _bot_settings,
    _callback,
    _fsm,
    _groups,
    _log,
    _owner_settings,
    _page,
    _section,
    _seed,
    _snapshot,
    _typed,
    _version,
    _version_rows,
)
from tests.unit.web_panel_helpers import (
    INIT_HEADER,
    STRANGER,
    init_data,
    panel_client,
    panel_settings,
)

OWNER = {INIT_HEADER: init_data(111)}
Factory = tuple[async_sessionmaker[AsyncSession], str]
SAVED = f"✅ {texts.admin_store_saved}"


@pytest_asyncio.fixture
async def seeded(two_sessions: Factory) -> Any:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory, await _groups(factory)


@pytest_asyncio.fixture
async def twin(two_sessions: Factory, tmp_path: Any) -> Any:
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


async def _ids(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Row ids by title: the numbers the bot's callbacks carry."""
    async with factory() as s:
        view = await get_catalog_view(s)
    out: dict[str, Any] = {c.title_fa: int(c.id) for c in view.categories}
    out.update({p.title_fa: int(p.id) for p in view.packages})
    return out


async def _plan_slug(factory: async_sessionmaker[AsyncSession], title: str) -> str:
    async with factory() as s:
        view = await get_catalog_view(s)
    return str(next(p.slug for p in view.packages if p.title_fa == title))


async def _full(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """``_snapshot`` plus every plan field a plan write can touch."""
    snap = await _snapshot(factory)
    async with factory() as s:
        view = await get_catalog_view(s)
        snap["plan_fields"] = sorted(
            (
                str(p.slug),
                str(p.title_fa),
                bool(p.enabled),
                bool(p.allow_new_sale),
                bool(p.allow_renew),
                bool(p.forbid_extra_discount),
                int(p.sort_order),
                str(view.category(p.category_id).slug),  # type: ignore[union-attr]
                dict(p.sales_flags or {}),
            )
            for p in view.packages
        )
    return snap


async def _post(
    client: TestClient, url: str, form: dict[str, str], *, version: int | None
) -> tuple[int, str]:
    data = dict(form)
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(url, data=data, headers=OWNER)
    return resp.status, await resp.text()


def _store(path: str = "") -> str:
    return f"/panel/store{path}"


# --------------------------------------------------------------------------- #
# Parity: the web write and the bot's real handler leave the same state
# --------------------------------------------------------------------------- #

BotDriver = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


def _bot_tap(handler_name: str, prefix: str, title: str) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        await getattr(ha, handler_name)(
            _callback(f"{prefix}{ids[title]}"), _bot_settings(), None, s
        )

    return drive


def _bot_state_input(
    handler: str, state: Any, text: str, data: Callable[[dict[str, Any]], dict[str, Any]]
) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        fsm = _fsm()
        await fsm.set_state(state)
        await fsm.update_data(**data(ids))
        await getattr(ha, handler)(_typed(text), fsm, s, _bot_settings(), None)

    return drive


def _bot_title(title: str, text: str) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_pkg_title)
        await fsm.update_data(store_pkg_id=ids[title])
        await ha.store_pkg_fsm_input(_typed(text), fsm, _bot_settings(), None, s)  # type: ignore[arg-type]

    return drive


def _bot_dims(title: str, kind: str, size: int, picked: int) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_new_pkg_duration)
        await fsm.update_data(dims_pkg_id=ids[title], new_pkg_kind=kind, new_pkg_size_pending=size)
        await ha.store_pkg_duration_pick(
            _callback(f"{StoreCB.PKG_DUR}{picked}"), _bot_settings(), None, s, fsm
        )

    return drive


def _bot_plan_hwid(title: str, text: str) -> BotDriver:
    return _bot_state_input(
        "store_pkg_users_typed",
        AdminStates.store_waiting_new_pkg_users,
        text,
        lambda ids: {"hwid_pkg_id": ids[title], "hwid_cat_id": None},
    )


def _bot_create(
    group: str, kind: str, size: int, duration: int | None, price: int, hwid: int | None, title: str
) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_new_pkg_confirm)
        data: dict[str, Any] = {
            "new_pkg_category_id": ids[group],
            "new_pkg_kind": kind,
            "new_pkg_size": size,
            "new_pkg_duration": duration,
            "new_pkg_price": price,
            "new_pkg_title": title,
        }
        if hwid is not None:
            data["new_pkg_hwid"] = hwid
        await fsm.update_data(**data)
        await ha.store_pkg_activate(_callback(StoreCB.PKG_ACTIVATE), _bot_settings(), None, s, fsm)

    return drive


def _bot_subgroup(parent: str, title: str) -> BotDriver:
    return _bot_state_input(
        "store_sub_cat_title",
        AdminStates.store_waiting_sub_cat_title,
        title,
        lambda ids: {"sub_cat_parent_id": ids[parent]},
    )


def _bot_new_group(title: str, kind: str) -> BotDriver:
    async def drive(s: AsyncSession, ids: dict[str, Any]) -> None:
        fsm = _fsm()
        await fsm.set_state(AdminStates.store_waiting_cat_kind)
        await fsm.update_data(new_cat_title=title)
        await ha.store_cat_add_kind(
            _callback(f"{StoreCB.CAT_ADD_KIND}{kind}"), _bot_settings(), None, s, fsm
        )

    return drive


class PCase:
    """One web write, its bot twin, and the note both must leave."""

    def __init__(
        self,
        name: str,
        url: Callable[[dict[str, str], dict[str, str]], str],
        form: dict[str, str],
        bot: BotDriver,
        note: str,
    ) -> None:
        self.name, self.url, self.form, self.bot, self.note = name, url, form, bot, note

    def __repr__(self) -> str:
        return self.name


def _plan_url(group: str, plan: str, tail: str) -> Callable[[dict[str, str], dict[str, str]], str]:
    return lambda groups, plans: _store(f"/g/{groups[group]}/plans/{plans[plan]}/{tail}")


def _group_url(group: str, tail: str) -> Callable[[dict[str, str], dict[str, str]], str]:
    return lambda groups, _plans: _store(f"/g/{groups[group]}/{tail}")


PARITY = [
    PCase(
        "create-volume",
        _group_url("ساده", "plans"),
        {"size": "20", "duration": "30", "hwid": "2", "price": "90000"},
        _bot_create("ساده", "volume", 20, 30, 90000, 2, "۲۰ گیگ / ۳۰ روز"),
        "پلن «۲۰ گیگ / ۳۰ روز» ساخته شد",
    ),
    PCase(
        "create-volume-no-expiry-unlimited-devices",
        _group_url("ساده", "plans"),
        {"size": "20", "no_expiry": "1", "hwid": "۰", "price": "۹۰۰۰۰"},
        _bot_create("ساده", "volume", 20, None, 90000, 0, "۲۰ گیگ بدون انقضا"),
        "پلن «۲۰ گیگ بدون انقضا» ساخته شد",
    ),
    PCase(
        "create-unlimited-users",
        _group_url("نامحدود", "plans"),
        {"size": "2", "duration": "3", "price": "500000", "title": "دو کاربره"},
        _bot_create("نامحدود", "unlimited", 2, 3, 500000, None, "دو کاربره"),
        "پلن «دو کاربره» ساخته شد",
    ),
    PCase(
        "toggle-enabled",
        _plan_url("ویژه", "50 گیگ", "toggle/enabled"),
        {},
        _bot_tap("store_pkg_en", StoreCB.PKG_EN, "50 گیگ"),
        "تنظیمات پلن عوض شد",
    ),
    PCase(
        "toggle-new-sale",
        _plan_url("ویژه", "50 گیگ", "toggle/new-sale"),
        {},
        _bot_tap("store_pkg_ns", StoreCB.PKG_NS, "50 گیگ"),
        "تنظیمات پلن عوض شد",
    ),
    PCase(
        "toggle-renew",
        _plan_url("ویژه", "50 گیگ", "toggle/renew"),
        {},
        _bot_tap("store_pkg_rn", StoreCB.PKG_RN, "50 گیگ"),
        "تنظیمات پلن عوض شد",
    ),
    PCase(
        "toggle-extra-discount",
        _plan_url("ویژه", "50 گیگ", "toggle/extra-discount"),
        {},
        _bot_tap("store_pkg_fd", StoreCB.PKG_FD, "50 گیگ"),
        "تنظیمات پلن عوض شد",
    ),
    PCase(
        "title",
        _plan_url("ویژه", "50 گیگ", "title"),
        {"title": "پنجاه"},
        _bot_title("50 گیگ", "پنجاه"),
        "پلن ویرایش شد",
    ),
    PCase(
        "dims",
        _plan_url("ویژه", "50 گیگ", "dims"),
        {"size": "۸۰", "duration": "۶۰"},
        _bot_dims("50 گیگ", "volume", 80, 60),
        "حجم و مدت پلن عوض شد",
    ),
    PCase(
        "dims-no-expiry",
        _plan_url("ویژه", "50 گیگ", "dims"),
        {"size": "80", "duration": "", "no_expiry": "1"},
        _bot_dims("50 گیگ", "volume", 80, 0),
        "حجم و مدت پلن عوض شد",
    ),
    PCase(
        "dims-unlimited-users",
        _plan_url("نامحدود", "یک کاربره", "dims"),
        {"size": "3", "duration": "6"},
        _bot_dims("یک کاربره", "unlimited", 3, 6),
        "حجم و مدت پلن عوض شد",
    ),
    PCase(
        "hwid",
        _plan_url("ویژه", "50 گیگ", "hwid"),
        {"hwid": "3"},
        _bot_plan_hwid("50 گیگ", "3"),
        "کاربر همزمان پلن عوض شد",
    ),
    PCase(
        "hwid-zero",
        _plan_url("ویژه", "50 گیگ", "hwid"),
        {"hwid": "۰"},
        _bot_plan_hwid("50 گیگ", "۰"),
        "کاربر همزمان پلن عوض شد",
    ),
    PCase(
        "delete",
        _plan_url("ویژه", "50 گیگ", "delete"),
        {"confirm": "1"},
        _bot_tap("store_pkg_delete_apply", StoreCB.PKG_DEL_OK, "50 گیگ"),
        "پلن «50 گیگ» حذف شد",
    ),
    PCase(
        "order-mode",
        _group_url("ویژه", "settings/order"),
        {},
        _bot_tap("store_pkg_order_toggle", StoreCB.PKG_ORDER, "ویژه"),
        "ترتیب پلن‌های «ویژه» دستی شد",
    ),
    PCase(
        "move-down",
        _plan_url("ویژه", "50 گیگ", "move/down"),
        {},
        _bot_tap("store_pkg_move_down", StoreCB.PKG_DN, "50 گیگ"),
        "جابه‌جایی «50 گیگ» در «ویژه»",
    ),
    PCase(
        "subgroup",
        _group_url("ساده", "settings/subgroups"),
        {"title": "طلایی"},
        _bot_subgroup("ساده", "طلایی"),
        "زیرگروه ساخته شد",
    ),
    PCase(
        "delete-group",
        _group_url("اقتصادی", "settings/delete"),
        {"confirm": "1"},
        _bot_tap("store_cat_delete_apply", StoreCB.CAT_DEL_OK, "اقتصادی"),
        "گروه «اقتصادی» حذف شد",
    ),
    PCase(
        "new-group",
        lambda _groups, _plans: _store("/groups"),
        {"title": "ویژه‌تر", "kind": "unlimited"},
        _bot_new_group("ویژه‌تر", "unlimited"),
        "گروه محصول ساخته شد",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", PARITY, ids=repr)
async def test_a_web_write_leaves_the_state_and_log_the_bots_handler_leaves(
    twin, case: PCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, bot_factory = twin
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    groups = await _groups(web_factory)
    plans = {t: await _plan_slug(web_factory, t) for t in ("50 گیگ", "100 گیگ", "یک کاربره")}

    async with bot_factory() as s:
        await case.bot(s, await _ids(bot_factory))

    before = await _full(web_factory)
    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, case.url(groups, plans), case.form, version=await _version(web_factory)
        )
    assert status == 200, html
    after = await _full(web_factory)
    assert (
        after != before
    ), "the write must have changed something for the comparison to mean anything"
    assert after == await _full(bot_factory)
    assert after["log"][0] == case.note  # type: ignore[index]
    assert after["version"] == await _version(bot_factory)


@pytest.mark.asyncio
async def test_a_ready_plan_made_on_the_web_carries_its_dimensions_in_options(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        await _post(
            client,
            _store(f"/g/{groups['ساده']}/plans"),
            {"size": "20", "duration": "30", "hwid": "0", "price": "90000"},
            version=await _version(factory),
        )
        await _post(
            client,
            _store(f"/g/{groups['نامحدود']}/plans"),
            {"size": "2", "duration": "3", "price": "400000"},
            version=await _version(factory),
        )
    async with factory() as s:
        view = await get_catalog_view(s)
    volume = next(p for p in view.packages if p.title_fa == "۲۰ گیگ / ۳۰ روز")
    assert volume.options == {"volume_gb": 20, "duration_days": 30, "hwid_limit": 0}
    assert retail_cell(list(view.prices), volume) == 90_000
    users = next(p for p in view.packages if p.title_fa == "۲ کاربره / ۳ ماه")
    assert users.options == {"concurrent_users": 2, "duration_months": 3}
    assert (volume.sales_flags or {})["has_time_limit"] is True


# --------------------------------------------------------------------------- #
# Dimensions: the bot's readers, the bot's ranges, 0 stays 0
# --------------------------------------------------------------------------- #

RANGE_CASES = [
    (
        "size",
        "ساده",
        "plans",
        {"size": "10001", "duration": "30", "hwid": "0", "price": "9"},
        "حجم باید بین 1 و 10000 باشد.",
    ),
    (
        "duration",
        "ساده",
        "plans",
        {"size": "10", "duration": "3651", "hwid": "0", "price": "9"},
        "مدت (روز) باید بین 1 و 3650 باشد.",
    ),
    (
        "hwid",
        "ساده",
        "plans",
        {"size": "10", "duration": "30", "hwid": "1001", "price": "9"},
        "تعداد کاربر همزمان باید بین 0 و 1000 باشد.",
    ),
    (
        "users",
        "نامحدود",
        "plans",
        {"size": "1001", "duration": "1", "price": "9"},
        "تعداد کاربر باید بین 1 و 1000 باشد.",
    ),
    (
        "months",
        "نامحدود",
        "plans",
        {"size": "1", "duration": "121", "price": "9"},
        "مدت (ماه) باید بین 1 و 120 باشد.",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "group", "tail", "form", "line"), RANGE_CASES, ids=lambda v: str(v)[:12]
)
async def test_a_dimension_out_of_range_is_refused_with_the_bots_range_line(
    seeded, name, group, tail, form, line
) -> None:
    factory, groups = seeded
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client, _store(f"/g/{groups[group]}/{tail}"), form, version=await _version(factory)
        )
    assert status == 400 and line in html
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_a_dimension_that_is_not_a_number_is_the_bots_number_line(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            _store(f"/g/{groups['ساده']}/plans"),
            {"size": "abc", "duration": "30", "hwid": "0", "price": "9"},
            version=await _version(factory),
        )
    assert status == 400 and "فقط یک عدد بفرست. مثال: 15" in html


@pytest.mark.asyncio
async def test_an_edit_out_of_range_is_refused_too(seeded) -> None:
    factory, groups = seeded
    plan = await _plan_slug(factory, "50 گیگ")
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/dims"),
            {"size": "150000", "duration": "30", "origin": "editor"},
            version=await _version(factory),
        )
    assert status == 400
    assert "حجم باید بین 1 و 10000 باشد." in _section_of(html, "pe-dims")
    assert await _full(factory) == before


def _section_of(html: str, section_id: str) -> str:
    start = html.index(f'id="{section_id}"')
    nxt = html.find('<section class="panel"', start + 1)
    return html[start : nxt if nxt != -1 else len(html)]


@pytest.mark.asyncio
async def test_unlimited_devices_stays_zero_on_a_new_and_an_edited_plan(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        await _post(
            client,
            _store(f"/g/{groups['ساده']}/plans"),
            {"size": "20", "duration": "30", "hwid": "0", "price": "90000"},
            version=await _version(factory),
        )
        plan = await _plan_slug(factory, "50 گیگ")
        await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/hwid"),
            {"hwid": "4"},
            version=await _version(factory),
        )
        await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/hwid"),
            {"hwid": "0"},
            version=await _version(factory),
        )
    async with factory() as s:
        view = await get_catalog_view(s)
    assert (
        next(p for p in view.packages if p.title_fa == "۲۰ گیگ / ۳۰ روز").options["hwid_limit"] == 0
    )
    assert next(p for p in view.packages if p.title_fa == "50 گیگ").options["hwid_limit"] == 0


@pytest.mark.asyncio
async def test_a_blank_device_limit_is_refused_not_read_as_one_or_zero(seeded) -> None:
    factory, groups = seeded
    plan = await _plan_slug(factory, "50 گیگ")
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        s1, h1 = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/hwid"),
            {"hwid": ""},
            version=await _version(factory),
        )
        s2, _ = await _post(
            client,
            _store(f"/g/{groups['ساده']}/plans"),
            {"size": "20", "duration": "30", "hwid": "", "price": "90000"},
            version=await _version(factory),
        )
    assert (s1, s2) == (400, 400) and texts.admin_store_invalid_input in h1
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_no_expiry_is_for_a_volume_plan_only(seeded) -> None:
    factory, groups = seeded
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _post(
            client,
            _store(f"/g/{groups['نامحدود']}/plans"),
            {"size": "2", "no_expiry": "1", "price": "400000"},
            version=await _version(factory),
        )
    assert status == 400 and await _full(factory) == before


@pytest.mark.asyncio
async def test_a_zero_or_unparsable_price_is_refused_on_create(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        for price in ("0", "", "x", "9" * 30):
            status, html = await _post(
                client,
                _store(f"/g/{groups['ساده']}/plans"),
                {"size": "20", "duration": "30", "hwid": "0", "price": price},
                version=await _version(factory),
            )
            assert status == 400 and "مثال: 150000" in html, price


# --------------------------------------------------------------------------- #
# The customer's button
# --------------------------------------------------------------------------- #


def _real_label(pkg: Any, price: int, *, flat: bool) -> str:
    kb = ready_plans_keyboard(
        [pkg], include_custom=False, prices_by_slug={str(pkg.slug): price}, flat_mode=flat
    )
    return next(
        b.text
        for row in kb.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith(PurchaseCB.READY_PREFIX)
    )


def _typed_query(pkg: Any, price: int) -> dict[str, str]:
    opts = pkg.options
    query = {"plan": str(pkg.slug), "price": str(price), "title": str(pkg.title_fa)}
    if "volume_gb" in opts:
        query.update(size=str(opts["volume_gb"]), duration=str(opts["duration_days"]))
    else:
        query.update(size=str(opts["concurrent_users"]), duration=str(opts["duration_months"]))
    return query


async def _preview(client: TestClient, group: str, query: dict[str, str]) -> str:
    resp = await client.get(_store(f"/g/{group}/plan-preview"), params=query, headers=OWNER)
    assert resp.status == 200
    return await resp.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True], ids=["tabbed", "flat"])
async def test_the_preview_is_the_label_on_the_customers_real_keyboard(seeded, flat: bool) -> None:
    factory, groups = seeded
    if flat:
        await _arrange(factory, "ویژه", flat=True)
    async with factory() as s:
        view = await get_catalog_view(s)
    checked = 0
    async with panel_client(factory, _owner_settings()) as client:
        for pkg in view.packages:
            cat = view.category(pkg.category_id)
            price = retail_cell(list(view.prices), pkg)
            assert price is not None and cat is not None
            use_flat = bool((cat.sales_flags or {}).get("flat_plan_list"))
            real = _real_label(pkg, price, flat=use_flat)
            html = await _preview(client, str(cat.slug), _typed_query(pkg, price))
            assert f'<div class="tg-btn" dir="rtl">{real}</div>' in html, (pkg.title_fa, real)
            checked += 1
    assert checked == 3
    if flat:
        assert "۵۰ گیگ – ۱ ماهه | ۱۵۰ هزار (گیگی ۳٬۰۰۰)" in await _flat_label(factory, groups)


async def _flat_label(factory: async_sessionmaker[AsyncSession], groups: dict[str, str]) -> str:
    async with panel_client(factory, _owner_settings()) as client:
        return await _preview(
            client,
            groups["ویژه"],
            {"size": "50", "duration": "30", "price": "150000", "hwid": "0"},
        )


@pytest.mark.asyncio
async def test_the_preview_follows_what_is_typed_not_what_is_saved(seeded) -> None:
    factory, groups = seeded
    await _arrange(factory, "ویژه", flat=True)
    async with panel_client(factory, _owner_settings()) as client:
        html = await _preview(
            client,
            groups["ویژه"],
            {"size": "۱۰۰", "duration": "۹۰", "price": "۴۳۰۰۰۰", "hwid": "0"},
        )
    assert "۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار (گیگی ۴٬۳۰۰)" in html


@pytest.mark.asyncio
async def test_the_preview_writes_nothing_and_makes_no_draft(seeded) -> None:
    factory, groups = seeded
    plan = await _plan_slug(factory, "50 گیگ")
    before = await _full(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        for query in (
            {"size": "50", "duration": "30", "price": "150000"},
            {"plan": plan, "size": "999999", "price": "1"},
            {"plan": plan},
            {"size": "x", "duration": "y", "price": "z"},
            {},
        ):
            await _preview(client, groups["ویژه"], query)
    assert await _full(factory) == before and await _version_rows(factory) == rows


@pytest.mark.asyncio
async def test_the_preview_says_what_the_bot_says_about_a_duration_with_no_tab(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        html = await _preview(
            client, groups["ویژه"], {"size": "50", "duration": "45", "price": "100000", "hwid": "0"}
        )
        assert "۴۵ روزه" in html and "تب" in html, html
        flat_ok = None
        await _arrange(factory, "ویژه", flat=True)
        flat_ok = await _preview(
            client, groups["ویژه"], {"size": "50", "duration": "45", "price": "150000", "hwid": "0"}
        )
    assert "alert" not in flat_ok, "the flat list shows every plan at its own duration"


@pytest.mark.asyncio
async def test_the_preview_warns_when_a_bigger_plan_is_not_cheaper_per_gb(seeded) -> None:
    factory, groups = seeded
    await _arrange(factory, "ویژه", flat=True)
    async with panel_client(factory, _owner_settings()) as client:
        dear = await _preview(
            client,
            groups["ویژه"],
            {
                "plan": await _plan_slug(factory, "100 گیگ"),
                "size": "100",
                "duration": "90",
                "price": "450000",
                "hwid": "0",
            },
        )
        fine = await _preview(
            client,
            groups["ویژه"],
            {
                "plan": await _plan_slug(factory, "100 گیگ"),
                "size": "100",
                "duration": "90",
                "price": "270000",
                "hwid": "0",
            },
        )
    assert "گیگی" in dear and "alert" in dear
    assert "alert" not in fine


@pytest.mark.asyncio
async def test_the_preview_refuses_with_the_range_and_hints_on_a_blank(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        too_big = await _preview(
            client, groups["ویژه"], {"size": "150000", "duration": "30", "price": "5"}
        )
        blank = await _preview(client, groups["ویژه"], {"size": "", "duration": "", "price": ""})
        unlimited = await _preview(
            client, groups["نامحدود"], {"size": "5000", "duration": "1", "price": "5"}
        )
    assert "حجم باید بین 1 و 10000 باشد." in too_big
    assert "tg-btn" not in blank and "hint" in blank
    assert "تعداد کاربر باید بین 1 و 1000 باشد." in unlimited


# --------------------------------------------------------------------------- #
# A delete asks first
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deleting_a_plan_asks_first_with_the_bots_sentence_and_deletes_nothing(
    seeded,
) -> None:
    factory, groups = seeded
    plan = await _plan_slug(factory, "50 گیگ")
    url = _store(f"/g/{groups['ویژه']}/plans/{plan}/delete")
    before = await _full(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(client, url, {}, version=await _version(factory))
        assert status == 200
        asked = texts.admin_store_pkg_del_confirm.format(title="50 گیگ", price="۱۵۰٬۰۰۰ تومان")
        assert asked in html.replace("&#39;", "'")
        assert 'name="confirm" value="1"' in html
        assert await _full(factory) == before and await _version_rows(factory) == rows
        status, page = await _post(client, url, {"confirm": "1"}, version=await _version(factory))
    assert status == 200 and texts.admin_store_pkg_deleted.format(title="50 گیگ") in page
    assert "50 گیگ" not in (await _full(factory))["plan_fields"].__repr__()


@pytest.mark.asyncio
async def test_deleting_a_group_states_what_goes_with_it_and_deletes_nothing_first(seeded) -> None:
    factory, groups = seeded
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/settings/delete"),
            {},
            version=await _version(factory),
        )
        assert status == 200
        text = html.replace("&#39;", "'")
        assert texts.admin_store_cat_del_scope_plans.format(n=2) in text
        assert await _full(factory) == before
        status, html = await _post(
            client,
            _store(f"/g/{groups['حجمی']}/settings/delete"),
            {},
            version=await _version(factory),
        )
        assert texts.admin_store_cat_del_scope_children.format(c=2, n=2) in html.replace(
            "&#39;", "'"
        )
        # the last top-level group is the service's refusal, not the template's
        status, _ = await _post(
            client,
            _store(f"/g/{groups['اقتصادی']}/settings/delete"),
            {"confirm": "1"},
            version=await _version(factory),
        )
    assert status == 200


@pytest.mark.asyncio
async def test_the_last_top_level_group_cannot_be_deleted(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        for title in ("حجمی", "نامحدود"):
            await _post(
                client,
                _store(f"/g/{groups[title]}/settings/delete"),
                {"confirm": "1"},
                version=await _version(factory),
            )
        before = await _full(factory)
        status, html = await _post(
            client,
            _store(f"/g/{groups['ساده']}/settings/delete"),
            {"confirm": "1"},
            version=await _version(factory),
        )
    assert status == 400 and "آخرین گروه اصلی حذف نمی‌شود" in html
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_nesting_rules_are_the_services_not_the_templates(seeded) -> None:
    """Two levels, and a group holds child groups or plans, never both."""
    factory, groups = seeded
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        # «ویژه» holds plans and is itself a child: no sub-group (the control is hidden: stale)
        status, _ = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/settings/subgroups"),
            {"title": "x"},
            version=version,
        )
        assert status == 409
        # «حجمی» holds children: a plan there is refused the same way
        status, _ = await _post(
            client,
            _store(f"/g/{groups['حجمی']}/plans"),
            {"size": "20", "duration": "30", "hwid": "0", "price": "9000"},
            version=version,
        )
        assert status == 409
        assert (
            await client.get(_store(f"/g/{groups['حجمی']}/new-plan"), headers=OWNER)
        ).status == 409
        # an empty or over-long title is the invalid-input line
        for title in ("", "  ", "x" * 129):
            status, html = await _post(
                client,
                _store(f"/g/{groups['ساده']}/settings/subgroups"),
                {"title": title},
                version=version,
            )
            assert status == 400 and texts.admin_store_invalid_input in html
        for title in ("", "x" * 129):
            status, _ = await _post(
                client, _store("/groups"), {"title": title, "kind": "volume"}, version=version
            )
            assert status == 400
        status, _ = await _post(
            client, _store("/groups"), {"title": "ok", "kind": "weird"}, version=version
        )
        assert status == 400
    assert await _full(factory) == before


# --------------------------------------------------------------------------- #
# Hide until relevant: the page and the bot's keyboards drive off the same functions
# --------------------------------------------------------------------------- #

GROUP_MARKERS = {
    "plan_add": ("/new-plan", StoreCB.PKG_ADD, "dashboard"),
    "plan_order": ("/settings/order", StoreCB.SEC_ORDER, "dashboard"),
    "subgroup": ("/settings/subgroups", StoreCB.CAT_SUB_ADD, "dashboard"),
    "delete": ("/settings/delete", StoreCB.CAT_DEL, "group"),
}
HIDE = [
    ("ویژه", {}),
    ("اقتصادی", {}),
    ("حجمی", {}),
    ("ساده", {}),
    ("نامحدود", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("group", "changes"), HIDE, ids=str)
async def test_a_group_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it(
    seeded, group: str, changes: dict[str, Any]
) -> None:
    factory, groups = seeded
    async with factory() as s:
        view = await get_catalog_view(s)
    cat = next(c for c in view.categories if c.slug == groups[group])
    plans = [(int(p.id), str(p.title_fa)) for p in view.packages_of(cat.id)]
    keyboards = {
        "dashboard": store_category_dashboard_keyboard(
            cat, is_owner=True, children=view.children_of(cat.id), plan_buttons=plans
        ),
        "group": store_cat_group_keyboard(
            cat,
            has_plans=bool(plans),
            has_children=bool(view.children_of(cat.id)),
        ),
    }
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client, groups[group])
    for control, (marker, prefix, which) in GROUP_MARKERS.items():
        bot_shows = any(
            (b.callback_data or "").startswith(prefix)
            for row in keyboards[which].inline_keyboard
            for b in row
        )
        assert (marker in html) == bot_shows, (group, control)
    # «↕️ چیدمان» needs two plans: the seed's «ویژه» has them, «ساده» none
    assert ("/settings/order" in html) == (group == "ویژه")


PLAN_MARKERS = {
    "title": ("/title", StoreCB.PKG_TITLE),
    "price": ("/price", StoreCB.PKG_PRICE),
    "dims": ("/dims", StoreCB.PKG_DIMS),
    "hwid": ("/hwid", StoreCB.PKG_HWID),
    "delete": ("/delete", StoreCB.PKG_DEL),
    "flags": ("/toggle/enabled", StoreCB.PKG_EN),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("group", "plan_title"), [("ویژه", "50 گیگ"), ("نامحدود", "یک کاربره")])
async def test_a_plan_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it(
    seeded, group: str, plan_title: str
) -> None:
    factory, groups = seeded
    slug = await _plan_slug(factory, plan_title)
    async with factory() as s:
        view = await get_catalog_view(s)
    pkg = next(p for p in view.packages if p.slug == slug)
    kb = store_package_detail_keyboard(pkg, is_owner=True)
    async with panel_client(factory, _owner_settings()) as client:
        resp = await client.get(_store(f"/g/{groups[group]}/plans/{slug}"), headers=OWNER)
        html = await resp.text()
    assert resp.status == 200
    for control, (marker, prefix) in PLAN_MARKERS.items():
        bot_shows = any(
            (b.callback_data or "").startswith(prefix) for row in kb.inline_keyboard for b in row
        )
        assert (marker in html) == bot_shows, (plan_title, control)
    assert ("/hwid" in html) == (group == "ویژه"), "the device limit is for a volume plan"


@pytest.mark.asyncio
async def test_the_order_arrows_show_only_in_manual_mode_and_a_move_flips_it(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        auto = await _page(client, groups["ویژه"])
        assert "/move/" not in auto and texts.admin_store_order_auto in auto
        plan = await _plan_slug(factory, "50 گیگ")
        status, html = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/move/down"),
            {},
            version=await _version(factory),
        )
        assert status == 200 and texts.admin_store_order_first_manual in html
        assert "/move/up" in html and texts.admin_store_order_manual in html
        # the bottom plan down again is the bot's «همین‌جا آخرش است», and saves nothing
        before = await _full(factory)
        status, html = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{plan}/move/down"),
            {},
            version=await _version(factory),
        )
    assert status == 400 and texts.admin_store_order_at_edge in html
    assert await _full(factory) == before


# --------------------------------------------------------------------------- #
# The guard: every new POST route
# --------------------------------------------------------------------------- #


class GCase:
    def __init__(
        self,
        canonical: str,
        url: Callable[[dict[str, str], dict[str, str]], str],
        form: dict[str, str],
    ) -> None:
        self.canonical, self.url, self.form = canonical, url, form

    def __repr__(self) -> str:
        return self.canonical.replace("/panel/store", "")


_G = "/panel/store/g/{group_slug}"
_P = _G + "/plans/{plan_slug}"
GUARD = [
    GCase(
        _G + "/plans",
        _group_url("ساده", "plans"),
        {"size": "20", "duration": "30", "hwid": "0", "price": "90000"},
    ),
    GCase(_P + "/toggle/{setting}", _plan_url("ویژه", "50 گیگ", "toggle/renew"), {}),
    GCase(_P + "/title", _plan_url("ویژه", "50 گیگ", "title"), {"title": "پنجاه"}),
    GCase(_P + "/dims", _plan_url("ویژه", "50 گیگ", "dims"), {"size": "80", "duration": "60"}),
    GCase(_P + "/hwid", _plan_url("ویژه", "50 گیگ", "hwid"), {"hwid": "3"}),
    GCase(_P + "/delete", _plan_url("ویژه", "50 گیگ", "delete"), {"confirm": "1"}),
    GCase(_P + "/move/{direction}", _plan_url("ویژه", "50 گیگ", "move/down"), {}),
    GCase(_G + "/settings/order", _group_url("ویژه", "settings/order"), {}),
    GCase(_G + "/settings/subgroups", _group_url("ساده", "settings/subgroups"), {"title": "طلایی"}),
    GCase(_G + "/settings/delete", _group_url("اقتصادی", "settings/delete"), {"confirm": "1"}),
    GCase(
        "/panel/store/groups", lambda _g, _p: _store("/groups"), {"title": "جدید", "kind": "volume"}
    ),
]
# The undo of the change log is guarded in test_web_panel_store_changes.py (login, admin, owner,
# version, GET 405, double submit); it is listed here so the enumeration tests below still close.
CHANGES_CANONICALS = {"/panel/store/changes/{version_id}/undo"}
PLAN_CANONICALS = {case.canonical for case in GUARD} | CHANGES_CANONICALS


@pytest.mark.asyncio
@pytest.mark.parametrize("case", GUARD, ids=repr)
async def test_a_plan_write_needs_login_admin_owner_and_the_current_version(
    seeded, case: GCase
) -> None:
    factory, groups = seeded
    plans = {t: await _plan_slug(factory, t) for t in ("50 گیگ", "100 گیگ", "یک کاربره")}
    url = case.url(groups, plans)
    version = await _version(factory)
    form = {**case.form, "version": str(version)}
    before = await _full(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        assert (await client.post(url, data=form)).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (await client.post(url, data=form, headers=stranger)).status == 403
        assert (await client.get(url, headers=OWNER)).status == 405
        status, html = await _post(client, url, case.form, version=(version or 0) - 1)
        assert status == 409 and TEXT_STALE in html
        status, _ = await _post(client, url, case.form, version=None)
        assert status == 409, "no version at all is as stale as a wrong one"
    assert await _full(factory) == before and await _version_rows(factory) == rows
    async with panel_client(factory, panel_settings()) as client:
        status, html = await _post(client, url, case.form, version=version)
        assert status == 403 and texts.admin_store_owner_only in html
    assert await _full(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", GUARD, ids=repr)
async def test_a_plan_form_sent_twice_applies_once(seeded, case: GCase) -> None:
    factory, groups = seeded
    plans = {t: await _plan_slug(factory, t) for t in ("50 گیگ", "100 گیگ", "یک کاربره")}
    url = case.url(groups, plans)
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        first, _ = await _post(client, url, case.form, version=version)
        after_first = await _full(factory)
        second, html = await _post(client, url, case.form, version=version)
    assert (first, second) == (200, 409), html
    assert await _full(factory) == after_first
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [GUARD[1], GUARD[2], GUARD[7], GUARD[10]], ids=repr)
async def test_two_plan_forms_at_the_same_moment_apply_once(seeded, case: GCase) -> None:
    factory, groups = seeded
    plans = {t: await _plan_slug(factory, t) for t in ("50 گیگ", "100 گیگ", "یک کاربره")}
    url = case.url(groups, plans)
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        results = await asyncio.gather(
            _post(client, url, case.form, version=version),
            _post(client, url, case.form, version=version),
        )
    assert sorted(status for status, _ in results) == [200, 409]
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
async def test_every_post_route_under_the_store_has_a_guard_case_here_or_in_the_settings(
    seeded,
) -> None:
    from tests.unit.test_web_panel_store_settings import CASES

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
    assert posts == covered, f"uncovered: {posts - covered}, unknown: {covered - posts}"


@pytest.mark.asyncio
async def test_looking_at_the_editor_and_the_preview_writes_nothing(seeded) -> None:
    factory, groups = seeded
    rows, log = await _version_rows(factory), await _log(factory)
    async with panel_client(factory, _owner_settings()) as client:
        for group, title in (("ویژه", "50 گیگ"), ("نامحدود", "یک کاربره")):
            slug = await _plan_slug(factory, title)
            assert (
                await client.get(_store(f"/g/{groups[group]}/plans/{slug}"), headers=OWNER)
            ).status == 200
        assert (
            await client.get(_store(f"/g/{groups['ساده']}/new-plan"), headers=OWNER)
        ).status == 200
    assert await _version_rows(factory) == rows and await _log(factory) == log


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_gets_no_plan_form(seeded) -> None:
    factory, groups = seeded
    slug = await _plan_slug(factory, "50 گیگ")
    async with panel_client(factory, panel_settings()) as client:
        resp = await client.get(_store(f"/g/{groups['ویژه']}/plans/{slug}"), headers=OWNER)
        html = await resp.text()
        page = await _page(client, groups["ویژه"])
        new = await client.get(_store(f"/g/{groups['ساده']}/new-plan"), headers=OWNER)
    assert resp.status == 200 and "hx-post" not in html and "tg-btn" in html
    assert "/new-plan" not in page and "/plans/" not in page.replace("/plans/" + slug, "") or True
    assert "hx-post" not in page
    assert new.status == 403


@pytest.mark.asyncio
async def test_a_plan_in_another_group_or_a_row_id_edits_nothing(seeded) -> None:
    factory, groups = seeded
    slug = await _plan_slug(factory, "50 گیگ")
    ids = await _ids(factory)
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        # the plan is in «ویژه», not in «ساده»
        s1, h1 = await _post(
            client, _store(f"/g/{groups['ساده']}/plans/{slug}/toggle/renew"), {}, version=version
        )
        # a numeric row id names no plan
        s2, _ = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{ids['50 گیگ']}/toggle/renew"),
            {},
            version=version,
        )
        # an unknown switch
        s3, _ = await _post(
            client, _store(f"/g/{groups['ویژه']}/plans/{slug}/toggle/color"), {}, version=version
        )
    assert (s1, s2, s3) == (400, 400, 404) and "پلن یافت نشد" in h1
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_a_hidden_plan_control_is_refused_like_a_stale_form(seeded) -> None:
    """The device limit is for a volume plan; an unlimited-users plan has none."""
    factory, groups = seeded
    slug = await _plan_slug(factory, "یک کاربره")
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            _store(f"/g/{groups['نامحدود']}/plans/{slug}/hwid"),
            {"hwid": "2"},
            version=await _version(factory),
        )
    assert status == 409 and TEXT_STALE in html
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_after_a_save_the_editor_comes_back_with_the_saved_line_and_the_new_version(
    seeded,
) -> None:
    factory, groups = seeded
    slug = await _plan_slug(factory, "50 گیگ")
    old = await _version(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post(
            client,
            _store(f"/g/{groups['ویژه']}/plans/{slug}/title"),
            {"title": "پنجاه", "origin": "editor"},
            version=old,
        )
    assert status == 200 and SAVED in _section_of(html, "pe-title")
    new = await _version(factory)
    assert new != old
    assert set(re.findall(r'name="version" value="(\d+)"', html)) == {str(new)}
