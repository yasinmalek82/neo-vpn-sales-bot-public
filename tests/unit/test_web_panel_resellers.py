"""«🏪 نمایندگان» on the Mini App (step 4 of the panel plan): the bot's reseller controls on the web.

What these defend, each with a test that goes red when the real line is broken:

* the list and the sheet show the bot's own figures, a plan's agreed price beside the retail
  price of the same cell, found by the plan's slug (a catalog edit that re-mints row ids moves
  nothing), and a stored price whose plan left the catalog is shown, not hidden;
* every write leaves the rows the bot's real handler leaves for the same input;
* the price a reseller is quoted and charged follows a web edit; a price above retail is
  charged at retail and the sheet says so (⚠️ computed by the service, not the template);
* the bot's parsing and refusal lines, nothing written on a refusal (a superscript digit, a
  number past 64 bits, zero, a percent outside 1..90);
* the percent cut asks first with the bot's preview, applies exactly that set or nothing, applies
  once, and its undo restores exactly the prices before it;
* a form carries what it was drawn from: stale and double-submitted forms are refused and an edit
  made in the bot meanwhile is never overwritten;
* every POST under ``/panel/resellers`` obeys one guard and a route with no case here fails the build;
  a customer or a reseller cannot open any of it; no route grants or removes the role.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from markupsafe import escape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import admin as bot_admin
from bot.texts import texts
from database.base import Base
from database.models import User
from services.reseller import (
    clear_plan_price,
    quote_custom_volume,
    quote_ready_plan,
    set_plan_price,
    set_rate,
)
from services.reseller_admin import (
    TEXT_STALE_RESELLER,
)
from services.reseller_sheet import TEXT_CROSSED
from services.store_admin import (
    commit_and_activate,
    get_catalog_view,
    update_draft_package,
)
from services.store_pricing import DEFAULT_TIER, PREMIUM_TIER
from services.trial import get_reseller_trial_quota, reseller_month_sales
from tests.unit.web_panel_helpers import (
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    init_data,
    panel_client,
    panel_settings,
)
from tests.unit.web_personal_helpers import make_sale, make_trials
from tests.unit.web_reseller_helpers import (
    add_custom_template,
    add_shop,
    bot_clear,
    bot_cut,
    bot_fsm,
    bot_open,
    bot_rate,
    bot_set_price,
    bot_undo,
    dump_db,
    fake_bot_screens,
    package_row_ids,
    republish,
    said,
    seed_catalog,
    snapshot,
)

Factory = tuple[async_sessionmaker[AsyncSession], str]
ADMIN = {INIT_HEADER: init_data(ENV_ADMIN)}
NINES = "9" * 30
T = DEFAULT_TIER

LINE_INVALID = texts.admin_store_number_invalid.format(example="1200")
LINE_ZERO_PRICE = "قیمت باید بزرگ‌تر از صفر باشد."
LINE_PCT_INVALID = texts.admin_reseller_pct_invalid
LINE_NOTHING_TO_CUT = texts.admin_reseller_pct_nothing
LINE_NOTHING_TO_UNDO = texts.admin_reseller_nothing_to_undo


# --------------------------------------------------------------------------- #
# Fixtures and small helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _bot_screens_are_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bot_screens(monkeypatch)


@pytest_asyncio.fixture
async def env(two_sessions: Factory) -> AsyncIterator[SimpleNamespace]:
    factory, _ = two_sessions
    cat = await seed_catalog(factory)
    uid = await add_shop(factory)
    yield SimpleNamespace(factory=factory, cat=cat, uid=uid)


async def seed_prices(
    factory: async_sessionmaker[AsyncSession],
    uid: int,
    rows: list[tuple[str, int | None, str, int]],
) -> None:
    async with factory() as s:
        for slug, days, tier, price in rows:
            await set_plan_price(s, uid, slug, price, duration_days=days, tier=tier)
        await s.commit()


async def get_html(
    client: TestClient, uid: int, category: str | None = None, d: str = "", t: str = ""
) -> str:
    query = f"?c={category}&d={d}&t={t}" if category else ""
    resp = await client.get(f"/panel/resellers/{uid}{query}", headers=ADMIN)
    assert resp.status == 200, await resp.text()
    return await resp.text()


def cell_block(html: str, slug: str) -> str:
    start = html.index(f'id="cell-{slug}"')
    nxt = html.find('class="rs-cell"', start + 1)
    end = nxt if nxt != -1 else html.find("</section>", start)
    return html[start:end]


def hidden_fields(form_html: str) -> dict[str, str]:
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form_html))


def forms_of(block: str) -> list[str]:
    return re.findall(r"<form.*?</form>", block, flags=re.S)


def price_fields(html: str, slug: str) -> dict[str, str]:
    """The hidden fields of a cell's set-price form, exactly as the page drew them."""
    return hidden_fields(forms_of(cell_block(html, slug))[0])


def clear_fields(html: str, slug: str) -> dict[str, str]:
    return hidden_fields(forms_of(cell_block(html, slug))[1])


def form_by_action(html: str, suffix: str) -> dict[str, str]:
    for form in re.findall(r"<form.*?</form>", html, flags=re.S):
        if re.search(rf'hx-post="[^"]*{re.escape(suffix)}"', form):
            return hidden_fields(form)
    raise AssertionError(f"no form posts to ...{suffix}")


async def post(
    client: TestClient, uid: int, path: str, data: dict[str, str], headers: dict[str, str] = ADMIN
) -> tuple[int, str]:
    resp = await client.post(f"/panel/resellers/{uid}/{path}", data=data, headers=headers)
    return resp.status, await resp.text()


async def web_set_price(
    client: TestClient,
    env: SimpleNamespace,
    slug: str,
    value: str,
    *,
    category: str | None = None,
    d: str = "",
    t: str = "",
) -> tuple[int, str]:
    html = await get_html(client, env.uid, category or env.cat.tabbed, d, t)
    return await post(client, env.uid, "price", {**price_fields(html, slug), "value": value})


# --------------------------------------------------------------------------- #
# Read: the list and the sheet
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_list_shows_each_shop_with_the_bots_own_counts(env) -> None:
    other = await add_shop(env.factory, 7002, name="دومی")
    await add_shop(env.factory, 7003, name="مشتری عادی", reseller=False)
    await make_trials(env.factory, env.uid, count=2, quota=5)
    await make_sale(env.factory, env.uid, price=90_000)
    await make_sale(env.factory, env.uid, price=40_000)
    await seed_prices(env.factory, env.uid, [(env.cat.a50, 30, T, 120_000)])
    async with env.factory() as s:
        await set_rate(s, other, price_per_gb_toman=1_500)
        await s.commit()
        sales = await reseller_month_sales(s, env.uid)
        quota = await get_reseller_trial_quota(s, env.uid)
    assert (sales, quota.used, quota.quota) == (2, 2, 5)
    async with panel_client(env.factory) as client:
        html = await (await client.get("/panel/resellers", headers=ADMIN)).text()
    assert "موبایل" in html and "دومی" in html and "مشتری عادی" not in html
    assert "@shop7001" in html
    assert "۲ فروش • تست ۲/۵" in html
    assert "۱ قیمت پلن" in html
    first, second = html.split("دومی")[0], html.split("دومی")[1]
    assert "بدون نرخ گیگ" in first and "نرخ گیگ دارد" in second
    assert "فروش و تست‌ها مال همین ماه شمسی‌اند." in html


@pytest.mark.asyncio
async def test_the_list_is_empty_in_the_bots_words(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        html = await (await client.get("/panel/resellers", headers=ADMIN)).text()
    assert "هنوز نماینده‌ای نداری." in html


@pytest.mark.asyncio
async def test_the_list_pages(two_sessions: Factory) -> None:
    from services.reseller_sheet import PAGE_SIZE

    factory, _ = two_sessions
    for n in range(PAGE_SIZE + 2):
        await add_shop(factory, 8000 + n, name=f"شاپ{n:03d}")
    async with panel_client(factory) as client:
        first = await (await client.get("/panel/resellers", headers=ADMIN)).text()
        second = await (await client.get(f"/panel/resellers?o={PAGE_SIZE}", headers=ADMIN)).text()
    assert "شاپ000" in first and f"شاپ{PAGE_SIZE:03d}" not in first and "بعدی" in first
    assert f"شاپ{PAGE_SIZE:03d}" in second and "شاپ000" not in second and "قبلی" in second


def _cells(html: str) -> dict[str, tuple[str, str]]:
    """slug -> (retail text, agreed text) as the page prints them."""
    out = {}
    for slug in re.findall(r'id="cell-([^"]+)"', html):
        block = cell_block(html, slug)
        retail = re.search(r"خرده ([^<]+)<", block)
        # A priced cell reads «نماینده {price}»; only the unpriced line carries the colon.
        agreed = re.search(r"نماینده ([^<]+)<", block) or re.search(
            r"نماینده: (تعیین نشده)<", block
        )
        assert retail and agreed
        out[slug] = (retail.group(1), agreed.group(1))
    return out


@pytest.mark.asyncio
async def test_a_cell_shows_the_agreed_price_beside_the_retail_price_of_the_same_cell(env) -> None:
    await seed_prices(
        env.factory,
        env.uid,
        [
            (env.cat.a50, 30, T, 110_000),
            (env.cat.a50, 60, T, 240_000),
            (env.cat.a50, 30, PREMIUM_TIER, 170_000),
            (env.cat.f50, 60, T, 200_000),
        ],
    )
    c = env.cat
    async with panel_client(env.factory) as client:
        eco30 = _cells(await get_html(client, env.uid, c.tabbed, "30", T))
        eco60 = _cells(await get_html(client, env.uid, c.tabbed, "60", T))
        pre30 = _cells(await get_html(client, env.uid, c.tabbed, "30", PREMIUM_TIER))
        flat = _cells(await get_html(client, env.uid, c.flat))
    assert eco30 == {c.a50: ("۱۵۰٬۰۰۰", "۱۱۰٬۰۰۰"), c.a100: ("۲۵۰٬۰۰۰", "تعیین نشده")}
    assert eco60 == {
        c.a50: ("۲۷۰٬۰۰۰", "۲۴۰٬۰۰۰")
    }, "the 60-day tab holds only the plans priced there"
    assert pre30 == {c.a50: ("۲۰۰٬۰۰۰", "۱۷۰٬۰۰۰")}
    assert flat == {c.f10: ("۶۰٬۰۰۰", "تعیین نشده"), c.f50: ("۲۵۵٬۰۰۰", "۲۰۰٬۰۰۰")}


@pytest.mark.asyncio
async def test_the_sheet_holds_the_cells_the_bots_matrix_holds(env) -> None:
    """The bot's matrix context is the reference: same plans, same retail, same agreed."""
    await seed_prices(
        env.factory,
        env.uid,
        [(env.cat.a50, 30, T, 110_000), (env.cat.f10, 30, T, 50_000)],
    )
    c = env.cat
    tabs = [
        (c.tabbed, 30, T, "30", T),
        (c.tabbed, 60, T, "60", T),
        (c.tabbed, 30, PREMIUM_TIER, "30", PREMIUM_TIER),
        (c.flat, 30, T, "", ""),
    ]
    async with panel_client(env.factory) as client:
        for category, d, tier, d_arg, t_arg in tabs:
            async with env.factory() as s:
                state = bot_fsm()
                await bot_open(s, state, env.uid, category, d, tier)
                ctx = await bot_admin._reseller_matrix_context(s, state, env.uid)
            expected = {
                slug: (
                    f"{ctx['retail'][slug]:,}".replace(",", "٬").translate(
                        str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
                    ),
                    (
                        f"{ctx['agreed'][slug]:,}".replace(",", "٬").translate(
                            str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
                        )
                        if slug in ctx["agreed"]
                        else "تعیین نشده"
                    ),
                )
                for slug in (str(p.slug) for p in ctx["visible"])
            }
            assert _cells(await get_html(client, env.uid, category, d_arg, t_arg)) == expected
            assert expected, (category, d, tier)


@pytest.mark.asyncio
async def test_looking_writes_nothing_and_creates_no_row(env) -> None:
    before = await snapshot(env.factory, env.uid)
    assert before["rate"] is None
    async with panel_client(env.factory) as client:
        await client.get("/panel/resellers", headers=ADMIN)
        for category in (env.cat.tabbed, env.cat.flat):
            await get_html(client, env.uid, category)
        await get_html(client, env.uid, env.cat.tabbed, "60", PREMIUM_TIER)
        await get_html(client, env.uid, env.cat.tabbed, "garbage", "nonsense")
    assert await snapshot(env.factory, env.uid) == before


@pytest.mark.asyncio
async def test_a_person_who_is_not_a_reseller_has_no_sheet(env) -> None:
    plain = await add_shop(env.factory, 7100, reseller=False)
    async with panel_client(env.factory) as client:
        for path in (f"/panel/resellers/{plain}", "/panel/resellers/99999"):
            assert (await client.get(path, headers=ADMIN)).status == 404
        for suffix, form in (
            ("price", {"slug": env.cat.a50, "d": "30", "t": T, "value": "100000", "seen": "none"}),
            ("rate/gb", {"value": "1000", "seen": "none"}),
            ("cut", {"c": env.cat.tabbed, "d": "30", "t": T, "pct": "10"}),
            ("undo", {"c": env.cat.tabbed, "d": "30", "t": T, "seen": "1"}),
        ):
            status, _ = await post(client, plain, suffix, form)
            assert status == 404, suffix
    assert (await snapshot(env.factory, plain))["rate"] is None
    assert (await snapshot(env.factory, plain))["prices"] == []


@pytest.mark.asyncio
async def test_a_price_for_a_plan_that_left_the_catalog_is_shown_not_hidden(env) -> None:
    await seed_prices(
        env.factory,
        env.uid,
        [
            ("long-gone-plan", 30, T, 77_000),  # the plan no longer exists
            (env.cat.a50, 90, T, 130_000),  # the plan exists, its 90-day tab does not
            (env.cat.a50, 30, T, 110_000),
        ],
    )
    async with panel_client(env.factory) as client:
        html = await get_html(client, env.uid, env.cat.tabbed)
    assert "قیمت‌های بی‌جا" in html
    assert "long-gone-plan" in html and "پلن حذف‌شده" in html and "۷۷٬۰۰۰" in html
    assert "۱۳۰٬۰۰۰" in html.split("قیمت‌های بی‌جا")[1], "the 90-day price of a live plan is listed"
    assert (
        "۱۱۰٬۰۰۰" not in html.split("قیمت‌های بی‌جا")[1]
    ), "a price that has a cell is not an orphan"


@pytest.mark.asyncio
async def test_names_and_titles_are_escaped(env) -> None:
    evil = '<script>alert("x")</script>'
    async with env.factory() as s:
        shop = await s.get(User, env.uid)
        shop.first_name = evil
        shop.username = "a<b>"
        view = await get_catalog_view(s)
        pkg = next(p for p in view.packages if str(p.slug) == env.cat.a50)
        await update_draft_package(s, int(pkg.id), title_fa=evil)
        await commit_and_activate(s, notes="x")
        await s.commit()
    await seed_prices(env.factory, env.uid, [(env.cat.a50, 30, T, 110_000)])
    async with panel_client(env.factory) as client:
        pages = [
            await (await client.get("/panel/resellers", headers=ADMIN)).text(),
            await get_html(client, env.uid, env.cat.tabbed),
        ]
        status, preview = await post(
            client, env.uid, "cut", {"c": env.cat.tabbed, "d": "30", "t": T, "pct": "10"}
        )
        assert status == 200
        pages.append(preview)
    for html in pages:
        assert "<script>alert" not in html and "a<b>" not in html
        assert "&lt;script&gt;" in html


def test_no_float_in_the_reseller_code() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in (
        "services/reseller_admin.py",
        "services/reseller_sheet.py",
        "services/reseller_edit.py",
        "web/routes/resellers.py",
    ):
        assert "float(" not in (root / name).read_text(encoding="utf-8"), name


# --------------------------------------------------------------------------- #
# Parity: the row the web leaves is the row the bot's real handler leaves
# --------------------------------------------------------------------------- #

Pair = tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]
Price = tuple[str, int | None, str, int]


@pytest_asyncio.fixture
async def twin(two_sessions: Factory, tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    """The web's database and a second one for the bot's handlers, seeded the same."""
    web_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        cat = await seed_catalog(web_factory)
        assert await seed_catalog(bot_factory) == cat, "same slugs on both sides"
        uid = await add_shop(web_factory)
        assert await add_shop(bot_factory) == uid
        yield SimpleNamespace(web=web_factory, bot=bot_factory, cat=cat, uid=uid)
    finally:
        await engine.dispose()


def sheet_args(category: str, d: int | None, tier: str, flat: bool) -> tuple[str, str, str]:
    """The query the page is opened with: a flat list has no tabs."""
    if flat:
        return category, "", ""
    return category, "n" if d is None else str(d), tier


async def w_price(client: TestClient, uid: int, cat_slug: str, slug: str, d, tier, text, flat=False):  # type: ignore[no-untyped-def]
    html = await get_html(client, uid, *sheet_args(cat_slug, d, tier, flat))
    return await post(client, uid, "price", {**price_fields(html, slug), "value": text})


async def w_clear(client: TestClient, uid: int, cat_slug: str, slug: str, d, tier, flat=False):  # type: ignore[no-untyped-def]
    html = await get_html(client, uid, *sheet_args(cat_slug, d, tier, flat))
    return await post(client, uid, "price/clear", clear_fields(html, slug))


async def w_rate(client: TestClient, uid: int, field: str, text: str) -> tuple[int, str]:
    html = await get_html(client, uid)
    return await post(
        client, uid, f"rate/{field}", {**form_by_action(html, f"/rate/{field}"), "value": text}
    )


async def w_cut(client: TestClient, uid: int, cat_slug: str, d, tier, pct, flat=False):  # type: ignore[no-untyped-def]
    """Both requests of a cut: the preview, then the confirm form exactly as the preview drew it."""
    html = await get_html(client, uid, *sheet_args(cat_slug, d, tier, flat))
    status, preview = await post(client, uid, "cut", {**form_by_action(html, "/cut"), "pct": pct})
    if status != 200:
        return status, preview
    return await post(client, uid, "cut", form_by_action(preview, "/cut"))


async def w_undo(client: TestClient, uid: int, cat_slug: str, d, tier, flat=False):  # type: ignore[no-untyped-def]
    html = await get_html(client, uid, *sheet_args(cat_slug, d, tier, flat))
    return await post(client, uid, "undo", form_by_action(html, "/undo"))


def _prices(*rows: tuple[str, int | None, str, int]) -> list[tuple[str, int, str, int]]:
    return sorted((slug, -1 if d is None else d, tier, price) for slug, d, tier, price in rows)


def _pre_tabbed(c: Any) -> tuple[Price, ...]:
    return (
        (c.a50, 30, T, 140_000),
        (c.a100, 30, T, 230_000),
        (c.a50, 30, PREMIUM_TIER, 190_000),
        (c.a50, 60, T, 250_000),
        (c.f10, 30, T, 55_000),  # another group, on the same 30-day tab
    )


def _pre_flat(c: Any) -> tuple[Price, ...]:
    return (
        (c.f10, 30, T, 55_000),
        (c.f50, 60, T, 240_000),
        (c.a50, 30, T, 140_000),  # another group, on the same 30-day tab
    )


def _case(
    name: str,
    pre: Callable[[Any], tuple[Price, ...]],
    web: Callable[[TestClient, int, Any], Awaitable[Any]],
    bot: Callable[[AsyncSession, int, Any], Awaitable[Any]],
    prices: Callable[[Any], list[tuple[str, int, str, int]]],
    rate: tuple[int, int, int, int] | None = None,
) -> tuple[str, Callable[..., Any], Any, Any, Any, Any]:
    return (name, pre, web, bot, prices, rate)


def _none(_c: Any) -> tuple[Price, ...]:
    return ()


def _no_rows(_c: Any) -> list[tuple[str, int, str, int]]:
    return []


# Each case is (name, pre, web action, bot action, expected price rows, expected rate row).
PARITY: list[tuple[str, Any, Any, Any, Any, Any]] = [
    _case(
        "set a price, first time",
        _none,
        lambda cl, u, c: w_price(cl, u, c.tabbed, c.a50, 30, T, "120000"),
        lambda s, u, c: bot_set_price(s, u, c.tabbed, c.a50, 30, T, "120000"),
        lambda c: _prices((c.a50, 30, T, 120_000)),
    ),
    _case(
        "set a price over an existing one, typed in Persian digits",
        _pre_tabbed,
        lambda cl, u, c: w_price(cl, u, c.tabbed, c.a50, 30, T, "۱۳۰٬۰۰۰"),
        lambda s, u, c: bot_set_price(s, u, c.tabbed, c.a50, 30, T, "۱۳۰٬۰۰۰"),
        lambda c: _prices(
            (c.a50, 30, T, 130_000),
            (c.a100, 30, T, 230_000),
            (c.a50, 30, PREMIUM_TIER, 190_000),
            (c.a50, 60, T, 250_000),
            (c.f10, 30, T, 55_000),
        ),
    ),
    _case(
        "set a price on the premium tab",
        _pre_tabbed,
        lambda cl, u, c: w_price(cl, u, c.tabbed, c.a50, 30, PREMIUM_TIER, "180,000"),
        lambda s, u, c: bot_set_price(s, u, c.tabbed, c.a50, 30, PREMIUM_TIER, "180,000"),
        lambda c: _prices(
            (c.a50, 30, T, 140_000),
            (c.a100, 30, T, 230_000),
            (c.a50, 30, PREMIUM_TIER, 180_000),
            (c.a50, 60, T, 250_000),
            (c.f10, 30, T, 55_000),
        ),
    ),
    _case(
        "set a price on the 60-day tab",
        _none,
        lambda cl, u, c: w_price(cl, u, c.tabbed, c.a50, 60, T, "260000"),
        lambda s, u, c: bot_set_price(s, u, c.tabbed, c.a50, 60, T, "260000"),
        lambda c: _prices((c.a50, 60, T, 260_000)),
    ),
    _case(
        "set a flat-list price (the plan's own duration, economy), the bot's screen on a stale tab",
        _none,
        lambda cl, u, c: w_price(cl, u, c.flat, c.f50, 60, T, "200000", flat=True),
        # the owner came from the tabbed group's premium 30-day tab
        lambda s, u, c: bot_set_price(s, u, c.flat, c.f50, 30, PREMIUM_TIER, "200000"),
        lambda c: _prices((c.f50, 60, T, 200_000)),
    ),
    _case(
        "clear a price",
        _pre_tabbed,
        lambda cl, u, c: w_clear(cl, u, c.tabbed, c.a50, 30, T),
        lambda s, u, c: bot_clear(s, u, c.tabbed, c.a50, 30, T),
        lambda c: _prices(
            (c.a100, 30, T, 230_000),
            (c.a50, 30, PREMIUM_TIER, 190_000),
            (c.a50, 60, T, 250_000),
            (c.f10, 30, T, 55_000),
        ),
    ),
    _case(
        "clear a flat-list price",
        _pre_flat,
        lambda cl, u, c: w_clear(cl, u, c.flat, c.f50, 60, T, flat=True),
        lambda s, u, c: bot_clear(s, u, c.flat, c.f50, 30, PREMIUM_TIER),
        lambda c: _prices((c.f10, 30, T, 55_000), (c.a50, 30, T, 140_000)),
    ),
    _case(
        "rate per GB",
        _none,
        lambda cl, u, _c: w_rate(cl, u, "gb", "1500"),
        lambda s, u, _c: bot_rate(s, u, "gb", "1500"),
        _no_rows,
        (1500, 0, 0, 0),
    ),
    _case(
        "rate per day, zero is meaningful",
        _none,
        lambda cl, u, _c: w_rate(cl, u, "day", "۰"),
        lambda s, u, _c: bot_rate(s, u, "day", "۰"),
        _no_rows,
        (0, 0, 0, 0),
    ),
    _case(
        "monthly trial quota",
        _none,
        lambda cl, u, _c: w_rate(cl, u, "quota", "5"),
        lambda s, u, _c: bot_rate(s, u, "quota", "5"),
        _no_rows,
        (0, 0, 5, 0),
    ),
    _case(
        "extra trial price",
        _none,
        lambda cl, u, _c: w_rate(cl, u, "tprice", "20,000"),
        lambda s, u, _c: bot_rate(s, u, "tprice", "20,000"),
        _no_rows,
        (0, 0, 0, 20_000),
    ),
    _case(
        "two rate fields in turn keep each other",
        _none,
        lambda cl, u, _c: _both(w_rate(cl, u, "gb", "1500"), w_rate(cl, u, "quota", "3")),
        lambda s, u, _c: _both(bot_rate(s, u, "gb", "1500"), bot_rate(s, u, "quota", "3")),
        _no_rows,
        (1500, 0, 3, 0),
    ),
    _case(
        "10 percent off a tab: only this group's plans on this tab",
        _pre_tabbed,
        lambda cl, u, c: w_cut(cl, u, c.tabbed, 30, T, "10"),
        lambda s, u, c: bot_cut(s, u, c.tabbed, 30, T, "10"),
        lambda c: _prices(
            (c.a50, 30, T, 126_000),
            (c.a100, 30, T, 207_000),
            (c.a50, 30, PREMIUM_TIER, 190_000),
            (c.a50, 60, T, 250_000),
            (c.f10, 30, T, 55_000),
        ),
    ),
    _case(
        "10 percent off a flat list, the bot's screen on a stale premium tab",
        _pre_flat,
        lambda cl, u, c: w_cut(cl, u, c.flat, 30, T, "10", flat=True),
        lambda s, u, c: bot_cut(s, u, c.flat, 30, PREMIUM_TIER, "10"),
        lambda c: _prices((c.f10, 30, T, 49_000), (c.f50, 60, T, 216_000), (c.a50, 30, T, 140_000)),
    ),
    _case(
        "undo a tab's cut",
        _pre_tabbed,
        lambda cl, u, c: _both(w_cut(cl, u, c.tabbed, 30, T, "10"), w_undo(cl, u, c.tabbed, 30, T)),
        lambda s, u, c: _both(
            bot_cut(s, u, c.tabbed, 30, T, "10"), bot_undo(s, u, c.tabbed, 30, T)
        ),
        lambda c: _prices(
            (c.a50, 30, T, 140_000),
            (c.a100, 30, T, 230_000),
            (c.a50, 30, PREMIUM_TIER, 190_000),
            (c.a50, 60, T, 250_000),
            (c.f10, 30, T, 55_000),
        ),
    ),
    _case(
        "undo a flat list's cut",
        _pre_flat,
        lambda cl, u, c: _both(
            w_cut(cl, u, c.flat, 30, T, "10", flat=True), w_undo(cl, u, c.flat, 30, T, flat=True)
        ),
        lambda s, u, c: _both(
            bot_cut(s, u, c.flat, 30, PREMIUM_TIER, "10"), bot_undo(s, u, c.flat, 30, PREMIUM_TIER)
        ),
        lambda c: _prices((c.f10, 30, T, 55_000), (c.f50, 60, T, 240_000), (c.a50, 30, T, 140_000)),
    ),
]


async def _both(first: Awaitable[Any], second: Awaitable[Any]) -> Any:
    await first
    return await second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "pre", "web", "bot", "prices", "rate"), PARITY, ids=[row[0] for row in PARITY]
)
async def test_a_web_write_leaves_the_rows_the_bots_handler_leaves(
    twin, name, pre, web, bot, prices, rate
) -> None:
    c = twin.cat
    for factory in (twin.web, twin.bot):
        await seed_prices(factory, twin.uid, list(pre(c)))
    async with twin.bot() as s:
        await bot(s, twin.uid, c)
    async with panel_client(twin.web) as client:
        result = await web(client, twin.uid, c)
    if isinstance(result, tuple):
        assert result[0] == 200, result[1][:400]
    web_rows, bot_rows = await snapshot(twin.web, twin.uid), await snapshot(twin.bot, twin.uid)
    assert web_rows == bot_rows
    assert web_rows["prices"] == prices(c), "the rows themselves, not only their agreement"
    assert web_rows["rate"] == rate


# --------------------------------------------------------------------------- #
# Parity of the refusals: the bot's line, nothing written, on both sides
# --------------------------------------------------------------------------- #

BAD_PRICES = ["0", "۰", "-5", "abc", "12.5", NINES, "²", "", "   "]
BAD_RATES = [("gb", "²"), ("gb", NINES), ("day", "abc"), ("day", "-1"), ("tprice", NINES)] + [
    ("quota", str(2**31)),
    ("quota", "²"),
    ("quota", "99999999999"),
    ("quota", "x"),
]
BAD_PCT = ["0", "91", "100", "abc", "", "²", "10.5", "-5", NINES, "٪"]


def as_page(line: str) -> str:
    """How the page prints a bot line: the template escapes it."""
    return str(escape(line))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", BAD_PRICES)
async def test_a_bad_plan_price_is_refused_in_the_bots_words_and_writes_nothing(twin, text) -> None:
    c = twin.cat
    for factory in (twin.web, twin.bot):
        await seed_prices(factory, twin.uid, [(c.a50, 30, T, 140_000)])
    before = await snapshot(twin.web, twin.uid)
    async with twin.bot() as s:
        reply = await bot_set_price(s, twin.uid, c.tabbed, c.a50, 30, T, text)
    line = said(reply)
    assert line in (LINE_INVALID, LINE_ZERO_PRICE)
    async with panel_client(twin.web) as client:
        status, html = await w_price(client, twin.uid, c.tabbed, c.a50, 30, T, text)
    assert status == 400 and as_page(line) in html
    assert await snapshot(twin.web, twin.uid) == before == await snapshot(twin.bot, twin.uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "text"), BAD_RATES)
async def test_a_bad_rate_is_refused_in_the_bots_words_and_writes_nothing(
    twin, field, text
) -> None:
    before = await snapshot(twin.web, twin.uid)
    async with twin.bot() as s:
        line = said(await bot_rate(s, twin.uid, field, text))
    assert line == LINE_INVALID
    async with panel_client(twin.web) as client:
        status, html = await w_rate(client, twin.uid, field, text)
    assert status == 400 and as_page(line) in html
    assert await snapshot(twin.web, twin.uid) == before == await snapshot(twin.bot, twin.uid)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", BAD_PCT)
async def test_a_bad_percent_is_refused_in_the_bots_words_and_writes_nothing(twin, text) -> None:
    c = twin.cat
    for factory in (twin.web, twin.bot):
        await seed_prices(factory, twin.uid, [(c.a50, 30, T, 140_000)])
    before = await snapshot(twin.web, twin.uid)
    async with twin.bot() as s:
        line = said(await bot_cut(s, twin.uid, c.tabbed, 30, T, text))
    assert line == LINE_PCT_INVALID
    async with panel_client(twin.web) as client:
        status, html = await w_cut(client, twin.uid, c.tabbed, 30, T, text)
    assert status == 400 and as_page(line) in html
    assert await snapshot(twin.web, twin.uid) == before == await snapshot(twin.bot, twin.uid)


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True])
async def test_a_cut_with_nothing_to_cut_says_so_in_the_bots_words(twin, flat) -> None:
    """Other groups' prices on the same tab do not count as something to cut."""
    c = twin.cat
    other = (c.a50, 30, T, 140_000) if flat else (c.f10, 30, T, 55_000)
    for factory in (twin.web, twin.bot):
        await seed_prices(factory, twin.uid, [other])
    cat_slug, d, tier = (c.flat, 30, PREMIUM_TIER) if flat else (c.tabbed, 30, T)
    before = await snapshot(twin.web, twin.uid)
    async with twin.bot() as s:
        line = said(await bot_cut(s, twin.uid, cat_slug, d, tier, "10"))
    assert line == LINE_NOTHING_TO_CUT
    form = {"c": cat_slug, "d": "30", "t": T, "pct": "10"}
    async with panel_client(twin.web) as client:
        status, html = await post(client, twin.uid, "cut", form)
    assert status == 400 and as_page(line) in html
    assert await snapshot(twin.web, twin.uid) == before == await snapshot(twin.bot, twin.uid)


@pytest.mark.asyncio
async def test_an_undo_with_nothing_to_undo_says_so_in_the_bots_words(twin) -> None:
    c = twin.cat
    for factory in (twin.web, twin.bot):
        await seed_prices(factory, twin.uid, [(c.a50, 30, T, 140_000)])
    async with twin.bot() as s:
        callback = await bot_undo(s, twin.uid, c.tabbed, 30, T)
    assert callback.answer.await_args.args[0] == LINE_NOTHING_TO_UNDO
    async with panel_client(twin.web) as client:
        status, html = await post(
            client, twin.uid, "undo", {"c": c.tabbed, "d": "30", "t": T, "seen": "1"}
        )
    assert status == 409 and as_page(LINE_NOTHING_TO_UNDO) in html
    assert await snapshot(twin.web, twin.uid) == await snapshot(twin.bot, twin.uid)


# --------------------------------------------------------------------------- #
# What the reseller is charged after a web edit
# --------------------------------------------------------------------------- #

# Retail 2 000 toman per GB and 10 percent off from 60 days, as in the pricing tests.
CFG = {
    "currency": "IRT",
    "volume_plan": {
        "tiers": [{"max_gb": None, "price_per_gb_toman": 2000}],
        "duration_discounts": [{"min_days": 60, "percent": 10}],
    },
    "unlimited_plan": {
        "base_price_first_user_toman": 250_000,
        "additional_user_coefficients": [0.5],
        "duration_discounts": [],
    },
}


async def charged(
    factory: async_sessionmaker[AsyncSession],
    uid: int,
    slug: str,
    retail: int,
    *,
    d: int | None = 30,
    tier: str = T,
) -> tuple[int, bool]:
    """What `quote_ready_plan` charges, and whether the agreed price crossed over retail."""
    async with factory() as s:
        quote = await quote_ready_plan(
            s,
            user_id=uid,
            package_slug=slug,
            retail_price_toman=retail,
            duration_days=d,
            tier=tier,
        )
    return quote.price_toman, quote.crossed_over


async def checkout_price(
    factory: async_sessionmaker[AsyncSession], uid: int, slug: str, retail: int, d: int | None
) -> tuple[int, str | None]:
    """The price the purchase flow puts on an order (`apply_reseller_price`)."""
    from services.checkout import apply_reseller_price
    from services.pricing import PriceResult

    async with factory() as s:
        shop = await s.get(User, uid)
        priced = await apply_reseller_price(
            s,
            CFG,
            {
                "plan_type": "volume",
                "store_package_slug": slug,
                "sel_duration_days": d,
                "sel_tier": T,
                "title_fa": "x",
            },
            PriceResult(
                original_price=retail,
                discount_amount=0,
                final_price=retail,
                currency="IRT",
                meta={},
            ),
            shop,
        )
        await s.rollback()  # a crossed-over price enqueues a report; nothing here keeps it
    return priced.final_price, priced.meta.get("pricing_mode")


@pytest.mark.asyncio
async def test_the_price_set_on_the_web_is_the_price_the_reseller_is_charged(env) -> None:
    c, uid = env.cat, env.uid
    assert await charged(env.factory, uid, c.a50, 150_000) == (150_000, False), "no price: retail"
    async with panel_client(env.factory) as client:
        assert (await web_set_price(client, env, c.a50, "120000", d="30", t=T))[0] == 200
    assert await charged(env.factory, uid, c.a50, 150_000) == (120_000, False)
    assert await checkout_price(env.factory, uid, c.a50, 150_000, 30) == (120_000, "reseller")
    # the other cells of the same plan are still retail
    assert await charged(env.factory, uid, c.a50, 270_000, d=60) == (270_000, False)
    assert await charged(env.factory, uid, c.a50, 200_000, tier=PREMIUM_TIER) == (200_000, False)


@pytest.mark.asyncio
async def test_a_price_above_retail_is_charged_at_retail_and_the_sheet_says_so(env) -> None:
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        status, html = await web_set_price(client, env, c.a50, "180000", d="30", t=T)
        assert status == 200
        assert (await get_html(client, uid, c.tabbed, "30", T)).count(TEXT_CROSSED) == 1
        await web_set_price(client, env, c.a50, "100000", d="30", t=T)
        assert TEXT_CROSSED not in await get_html(client, uid, c.tabbed, "30", T)
    assert await charged(env.factory, uid, c.a50, 150_000) == (100_000, False)
    async with panel_client(env.factory) as client:
        await web_set_price(client, env, c.a50, "180000", d="30", t=T)
    assert await charged(env.factory, uid, c.a50, 150_000) == (150_000, True)
    assert (await checkout_price(env.factory, uid, c.a50, 150_000, 30))[0] == 150_000


@pytest.mark.asyncio
async def test_clearing_a_price_on_the_web_sends_the_reseller_back_to_retail(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 120_000), (c.f50, 60, T, 200_000)])
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        assert (await post(client, uid, "price/clear", clear_fields(html, c.a50)))[0] == 200
        html = await get_html(client, uid, c.flat)
        assert (await post(client, uid, "price/clear", clear_fields(html, c.f50)))[0] == 200
    assert await charged(env.factory, uid, c.a50, 150_000) == (150_000, False)
    assert await charged(env.factory, uid, c.f50, 255_000, d=60) == (255_000, False)
    assert (await snapshot(env.factory, uid))["prices"] == []


@pytest.mark.asyncio
async def test_a_cut_and_its_undo_on_the_web_move_what_the_reseller_is_charged(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 120_000), (c.a100, 30, T, 230_000)])
    async with panel_client(env.factory) as client:
        assert (await w_cut(client, uid, c.tabbed, 30, T, "10"))[0] == 200
        assert await charged(env.factory, uid, c.a50, 150_000) == (108_000, False)
        assert await charged(env.factory, uid, c.a100, 250_000) == (207_000, False)
        assert (await checkout_price(env.factory, uid, c.a50, 150_000, 30))[0] == 108_000
        assert (await w_undo(client, uid, c.tabbed, 30, T))[0] == 200
    assert await charged(env.factory, uid, c.a50, 150_000) == (120_000, False)
    assert await charged(env.factory, uid, c.a100, 250_000) == (230_000, False)


@pytest.mark.asyncio
async def test_a_flat_list_cut_changes_the_price_charged_at_the_plans_own_duration(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.f10, 30, T, 50_000), (c.f50, 60, T, 200_000)])
    async with panel_client(env.factory) as client:
        assert (await w_cut(client, uid, c.flat, None, T, "20", flat=True))[0] == 200
    assert await charged(env.factory, uid, c.f10, 60_000) == (40_000, False)
    assert await charged(env.factory, uid, c.f50, 255_000, d=60) == (160_000, False)


@pytest.mark.asyncio
async def test_a_rate_set_on_the_web_changes_the_custom_volume_quote(env) -> None:
    uid = env.uid

    async def quote() -> tuple[int, int, bool]:
        async with env.factory() as s:
            q = await quote_custom_volume(s, CFG, user_id=uid, volume_gb=50, duration_days=30)
        return q.price_toman, q.reseller_price_toman, q.crossed_over

    assert await quote() == (100_000, 100_000, False), "no rate card: retail"
    async with panel_client(env.factory) as client:
        assert (await w_rate(client, uid, "gb", "1500"))[0] == 200
        assert await quote() == (75_000, 75_000, False)
        assert (await w_rate(client, uid, "day", "100"))[0] == 200
        assert await quote() == (78_000, 78_000, False)
        assert (await w_rate(client, uid, "gb", "۳٬۰۰۰"))[0] == 200
    assert await quote() == (100_000, 153_000, True), "a rate above retail is charged at retail"
    async with env.factory() as s:
        from services.checkout import apply_reseller_price
        from services.pricing import PriceResult

        shop = await s.get(User, uid)
        priced = await apply_reseller_price(
            s,
            CFG,
            {"plan_type": "volume", "volume_gb": 50, "duration_days": 30},
            PriceResult(
                original_price=100_000,
                discount_amount=0,
                final_price=100_000,
                currency="IRT",
                meta={},
            ),
            shop,
        )
        await s.rollback()
    assert priced.final_price == 100_000


@pytest.mark.asyncio
async def test_the_trial_figures_set_on_the_web_are_the_ones_the_trial_code_reads(env) -> None:
    uid = env.uid
    async with panel_client(env.factory) as client:
        assert (await w_rate(client, uid, "quota", "4"))[0] == 200
        assert (await w_rate(client, uid, "tprice", "15000"))[0] == 200
    async with env.factory() as s:
        quota = await get_reseller_trial_quota(s, uid)
    assert quota.quota == 4 and quota.used == 0
    assert (await snapshot(env.factory, uid))["rate"] == (0, 0, 4, 15_000)


# --------------------------------------------------------------------------- #
# Slug identity: a catalog republish mints new row ids and moves nothing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_price_set_on_the_web_survives_a_republish_that_mints_new_row_ids(env) -> None:
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        await web_set_price(client, env, c.a50, "120000", d="30", t=T)
        html = await get_html(client, uid, c.flat)
        await post(client, uid, "price", {**price_fields(html, c.f50), "value": "200000"})
    ids_before = await package_row_ids(env.factory)
    await republish(env.factory)
    ids_after = await package_row_ids(env.factory)
    assert set(ids_before) == set(ids_after) and all(
        ids_after[slug] != ids_before[slug] for slug in ids_before
    ), "the republish really did mint new row ids for every plan"
    async with panel_client(env.factory) as client:
        eco30 = _cells(await get_html(client, uid, c.tabbed, "30", T))
        flat = _cells(await get_html(client, uid, c.flat))
        assert eco30[c.a50] == ("۱۵۰٬۰۰۰", "۱۲۰٬۰۰۰")
        assert flat[c.f50] == ("۲۵۵٬۰۰۰", "۲۰۰٬۰۰۰")
        # and the sheet's forms still write to the same row
        status, _ = await web_set_price(client, env, c.a50, "110000", d="30", t=T)
        assert status == 200
    assert await charged(env.factory, uid, c.a50, 150_000) == (110_000, False)
    assert len((await snapshot(env.factory, uid))["prices"]) == 2, "one row per cell, no second row"
    async with panel_client(env.factory) as client:
        assert not (await get_html(client, uid, c.tabbed)).count("قیمت‌های بی‌جا")


@pytest.mark.asyncio
async def test_a_form_drawn_before_a_republish_still_writes_the_right_cell(env) -> None:
    """The form names the plan by slug, so a row id minted after it was drawn cannot misdirect it."""
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        fields = price_fields(html, c.a50)
        await republish(env.factory)
        status, _ = await post(client, uid, "price", {**fields, "value": "125000"})
    assert status == 200
    assert (await snapshot(env.factory, uid))["prices"] == [(c.a50, 30, T, 125_000)]


# --------------------------------------------------------------------------- #
# Forged and malformed forms: refused, nothing written, never a 500
# --------------------------------------------------------------------------- #

LONG = "x" * 5000
INT64_MAX = 2**63 - 1


def _forged_price_cases(c: Any, custom: str) -> list[tuple[str, str, dict[str, str], int]]:
    ok = {"seen": "none", "value": "100000"}
    return [
        (
            "a slug the catalog does not have",
            "price",
            {"slug": "nope", "d": "30", "t": T, **ok},
            409,
        ),
        ("a slug of 5000 characters", "price", {"slug": LONG, "d": "30", "t": T, **ok}, 409),
        (
            "a flat plan at a duration that is not its own",
            "price",
            {"slug": c.f10, "d": "60", "t": T, **ok},
            409,
        ),
        (
            "a flat plan at the other flat plan's duration",
            "price",
            {"slug": c.f50, "d": "30", "t": T, **ok},
            409,
        ),
        (
            "a flat plan at the premium tier",
            "price",
            {"slug": c.f10, "d": "30", "t": PREMIUM_TIER, **ok},
            409,
        ),
        ("a tab the group does not have", "price", {"slug": c.a50, "d": "90", "t": T, **ok}, 409),
        (
            "the no-expiry tab of a group without one",
            "price",
            {"slug": c.a50, "d": "n", "t": T, **ok},
            409,
        ),
        (
            "a plan on a tab it has no retail cell on",
            "price",
            {"slug": c.a100, "d": "60", "t": T, **ok},
            409,
        ),
        (
            "a plan on the premium tab it has no cell on",
            "price",
            {"slug": c.a100, "d": "30", "t": PREMIUM_TIER, **ok},
            409,
        ),
        ("a tier that does not exist", "price", {"slug": c.a50, "d": "30", "t": "gold", **ok}, 409),
        (
            "a duration that is not a number",
            "price",
            {"slug": c.a50, "d": "abc", "t": T, **ok},
            409,
        ),
        ("a negative duration", "price", {"slug": c.a50, "d": "-30", "t": T, **ok}, 409),
        ("a superscript duration", "price", {"slug": c.a50, "d": "²", "t": T, **ok}, 409),
        ("a Persian-digit duration", "price", {"slug": c.a50, "d": "۳۰", "t": T, **ok}, 409),
        ("a duration past 64 bits", "price", {"slug": c.a50, "d": NINES, "t": T, **ok}, 409),
        ("no duration at all", "price", {"slug": c.a50, "t": T, **ok}, 409),
        (
            "a custom-purchase plan, which has no price of its own",
            "price",
            {"slug": custom, "d": "30", "t": T, **ok},
            409,
        ),
        (
            "a clear for a slug the catalog does not have",
            "price/clear",
            {"slug": "nope", "d": "30", "t": T, "seen": "none"},
            409,
        ),
        (
            "a clear for a custom-purchase plan",
            "price/clear",
            {"slug": custom, "d": "30", "t": T, "seen": "none"},
            409,
        ),
        (
            "a clear on a tab the group does not have",
            "price/clear",
            {"slug": c.a50, "d": "90", "t": T, "seen": "none"},
            409,
        ),
        (
            "a cut for a group the catalog does not have",
            "cut",
            {"c": "nope", "d": "30", "t": T, "pct": "10"},
            409,
        ),
        (
            "a cut for a group of 5000 characters",
            "cut",
            {"c": LONG, "d": "30", "t": T, "pct": "10"},
            409,
        ),
        (
            "a cut on a tab the group does not have",
            "cut",
            {"c": c.tabbed, "d": "90", "t": T, "pct": "10"},
            409,
        ),
        (
            "a cut at a tier the group does not have",
            "cut",
            {"c": c.tabbed, "d": "30", "t": "gold", "pct": "10"},
            409,
        ),
        ("a cut with no group", "cut", {"d": "30", "t": T, "pct": "10"}, 409),
        (
            "a confirmed cut for a group the catalog does not have",
            "cut",
            {"c": "nope", "d": "30", "t": T, "pct": "10", "confirm": "1", "seen": "abc"},
            409,
        ),
        (
            "a confirmed cut on a tab the group does not have",
            "cut",
            {"c": c.tabbed, "d": "90", "t": T, "pct": "10", "confirm": "1", "seen": "abc"},
            409,
        ),
        (
            "an undo for a group the catalog does not have",
            "undo",
            {"c": "nope", "d": "30", "t": T, "seen": "1"},
            409,
        ),
        (
            "an undo on a tab the group does not have",
            "undo",
            {"c": c.tabbed, "d": "90", "t": T, "seen": "1"},
            409,
        ),
    ]


@pytest.mark.asyncio
async def test_a_forged_form_is_refused_and_writes_nothing(env) -> None:
    custom = await add_custom_template(env.factory, env.cat.tabbed)
    await seed_prices(env.factory, env.uid, [(env.cat.a50, 30, T, 140_000)])
    before = await snapshot(env.factory, env.uid)
    cases = _forged_price_cases(env.cat, custom)
    async with panel_client(env.factory) as client:
        for label, suffix, form, want in cases:
            status, html = await post(client, env.uid, suffix, form)
            assert status == want, (label, status, html[:300])
            assert TEXT_STALE_RESELLER in html, label
            assert await snapshot(env.factory, env.uid) == before, label


@pytest.mark.asyncio
async def test_a_custom_purchase_plan_is_not_on_the_sheet_and_has_no_cell(env) -> None:
    custom = await add_custom_template(env.factory, env.cat.tabbed)
    async with panel_client(env.factory) as client:
        html = await get_html(client, env.uid, env.cat.tabbed, "30", T)
    assert f'id="cell-{custom}"' not in html and "ساخت دلخواه" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["bogus", "GB", "gb ", "price_per_gb_toman", "0", "quota%2F"])
async def test_an_unknown_rate_field_is_the_invalid_line_and_writes_nothing(env, field) -> None:
    before = await snapshot(env.factory, env.uid)
    async with panel_client(env.factory) as client:
        resp = await client.post(
            f"/panel/resellers/{env.uid}/rate/{field}",
            data={"seen": "none", "value": "1000", "c": "", "d": "", "t": ""},
            headers=ADMIN,
        )
        html = await resp.text()
    assert resp.status == 400 and as_page(texts.admin_panel_invalid) in html
    assert await snapshot(env.factory, env.uid) == before


@pytest.mark.asyncio
async def test_over_long_fields_are_a_refusal_not_an_error(env) -> None:
    c, uid = env.cat, env.uid
    before = await snapshot(env.factory, uid)
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        fields = price_fields(html, c.a50)
        for name in ("seen", "d", "t"):
            status, _ = await post(client, uid, "price", {**fields, "value": "1000", name: LONG})
            assert status == 409, name
        for value in ("1" * 5000, "9" * 5000, "۱" * 5000):
            status, page = await post(client, uid, "price", {**fields, "value": value})
            assert status == 400 and as_page(LINE_INVALID) in page
        rate = form_by_action(await get_html(client, uid), "/rate/gb")
        status, page = await post(client, uid, "rate/gb", {**rate, "value": "1" * 5000})
        assert status == 400 and as_page(LINE_INVALID) in page
        cut = {"c": c.tabbed, "d": "30", "t": T}
        status, page = await post(client, uid, "cut", {**cut, "pct": "1" * 5000})
        assert status == 400 and as_page(LINE_PCT_INVALID) in page
    assert await snapshot(env.factory, uid) == before


@pytest.mark.asyncio
async def test_a_number_at_the_columns_bound_is_saved_and_one_past_it_is_the_invalid_line(
    env,
) -> None:
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        for value, want in (
            (str(INT64_MAX), 200),
            (str(INT64_MAX + 1), 400),
            (str(2**31 - 1), 200),
        ):
            status, _ = await web_set_price(client, env, c.a50, value, d="30", t=T)
            assert status == want, value
        # the rate figures: the trial quota is a 32-bit column, the rest are 64-bit
        for field, value, want in (
            ("quota", str(2**31 - 1), 200),
            ("quota", str(2**31), 400),
            ("gb", str(INT64_MAX), 200),
            ("gb", str(INT64_MAX + 1), 400),
            ("day", "3000000000", 200),
            ("tprice", str(INT64_MAX), 200),
        ):
            status, _ = await w_rate(client, uid, field, value)
            assert status == want, (field, value)
        # the biggest numbers the columns hold still draw
        assert (await client.get(f"/panel/resellers/{uid}", headers=ADMIN)).status == 200
        assert (
            await client.get(f"/panel/resellers/{uid}?c={c.tabbed}&d=30&t=economy", headers=ADMIN)
        ).status == 200
    rows = await snapshot(env.factory, uid)
    assert rows["rate"] == (INT64_MAX, 3_000_000_000, 2**31 - 1, INT64_MAX)
    assert rows["prices"] == [(c.a50, 30, T, 2**31 - 1)]


@pytest.mark.asyncio
async def test_a_person_who_is_not_a_reseller_has_no_route_to_write_to(env) -> None:
    plain = await add_shop(env.factory, 7100, reseller=False)
    forms = [
        ("price", {"slug": env.cat.a50, "d": "30", "t": T, "value": "100000", "seen": "none"}),
        ("price/clear", {"slug": env.cat.a50, "d": "30", "t": T, "seen": "none"}),
        ("rate/gb", {"value": "1000", "seen": "none"}),
        ("rate/day", {"value": "1000", "seen": "none"}),
        ("rate/quota", {"value": "3", "seen": "none"}),
        ("rate/tprice", {"value": "3", "seen": "none"}),
        ("cut", {"c": env.cat.tabbed, "d": "30", "t": T, "pct": "10"}),
        ("cut", {"c": env.cat.tabbed, "d": "30", "t": T, "pct": "10", "confirm": "1", "seen": "x"}),
        ("undo", {"c": env.cat.tabbed, "d": "30", "t": T, "seen": "1"}),
    ]
    async with panel_client(env.factory) as client:
        for target in (plain, 99999, 2**62):
            for suffix, form in forms:
                status, _ = await post(client, target, suffix, form)
                assert status == 404, (target, suffix, form)
            assert (await client.get(f"/panel/resellers/{target}", headers=ADMIN)).status == 404
    rows = await snapshot(env.factory, plain)
    assert rows["prices"] == [] and rows["rate"] is None and rows["ops"] == []
    async with env.factory() as s:
        assert (await s.get(User, plain)).is_reseller is False, "no route grants the role"


@pytest.mark.asyncio
async def test_a_user_id_past_64_bits_in_the_url_is_a_404(env) -> None:
    big = "9" * 30
    async with panel_client(env.factory) as client:
        assert (await client.get(f"/panel/resellers/{big}", headers=ADMIN)).status == 404
        assert (await client.get(f"/panel/customers/{big}", headers=ADMIN)).status == 404
        status, _ = await post(client, int(big), "rate/gb", {"seen": "none", "value": "1"})
        assert status == 404


@pytest.mark.asyncio
async def test_an_18_digit_id_matching_no_user_is_a_404_on_the_reseller_sheet(env) -> None:
    big_18 = "9" * 18
    async with panel_client(env.factory) as client:
        assert (await client.get(f"/panel/resellers/{big_18}", headers=ADMIN)).status == 404


@pytest.mark.asyncio
async def test_a_19_digit_id_past_63_bits_is_a_404_on_the_reseller_sheet(env) -> None:
    """19 nines is above 2^63: a bound one digit wider would hand it to the driver (a 500)."""
    async with panel_client(env.factory) as client:
        assert (await client.get(f"/panel/resellers/{'9' * 19}", headers=ADMIN)).status == 404


# --------------------------------------------------------------------------- #
# The percent cut: preview, then confirm by digest; undo scoped to the screen
# --------------------------------------------------------------------------- #


async def cut_preview(client: TestClient, uid: int, c: Any, d: str, t: str, pct: str = "10"):  # type: ignore[no-untyped-def]
    """The first request of a cut: (status, html) of the preview page."""
    return await post(client, uid, "cut", {"c": c, "d": d, "t": t, "pct": pct})


def preview_set(html: str) -> list[str]:
    """The «plan: before ← after» lines of a preview page, as the page prints them."""
    return re.findall(r"^[^\n<]+: [\d٬]+ ← [\d٬]+$", re.sub(r"<[^>]+>", "", html), flags=re.M)


@pytest.mark.asyncio
async def test_the_first_cut_request_writes_nothing_and_shows_the_bots_preview(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, _pre_tabbed(c))
    before = await dump_db(env.factory)
    async with panel_client(env.factory) as client:
        status, html = await cut_preview(client, uid, c.tabbed, "30", T)
    assert status == 200
    async with env.factory() as s:
        view = await get_catalog_view(s)
        title = {str(p.slug): str(p.title_fa) for p in view.packages}
    assert (
        as_page(
            texts.admin_reseller_pct_preview.format(
                lines="\n".join(
                    texts.admin_reseller_pct_preview_line.format(
                        plan=title[slug], before=b, after=a
                    )
                    for slug, b, a in (
                        (c.a50, "۱۴۰٬۰۰۰", "۱۲۶٬۰۰۰"),
                        (c.a100, "۲۳۰٬۰۰۰", "۲۰۷٬۰۰۰"),
                    )
                ),
                count="۲",
            )
        )
        in html
    ), "the preview is the bot's, word for word"
    assert hidden_fields(html)["confirm"] == "1" and hidden_fields(html)["seen"]
    assert await dump_db(env.factory) == before, "no row, no timestamp, no undo snapshot"


@pytest.mark.asyncio
async def test_the_confirm_applies_exactly_the_previewed_set(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, _pre_tabbed(c))
    async with panel_client(env.factory) as client:
        status, preview = await cut_preview(client, uid, c.tabbed, "30", T, "15")
        assert status == 200
        shown = preview_set(preview)
        assert len(shown) == 2
        status, done = await post(client, uid, "cut", hidden_fields(preview))
    assert status == 200
    assert as_page(texts.admin_reseller_pct_done.format(count="۲")) in done
    rows = (await snapshot(env.factory, uid))["prices"]
    assert rows == _prices(
        (c.a50, 30, T, 119_000),
        (c.a100, 30, T, 195_000),
        (c.a50, 30, PREMIUM_TIER, 190_000),
        (c.a50, 60, T, 250_000),
        (c.f10, 30, T, 55_000),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", ["changed", "removed"])
async def test_if_a_price_of_the_set_moved_between_preview_and_confirm_nothing_is_written(
    env, moved: str
) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000), (c.a100, 30, T, 230_000)])
    async with panel_client(env.factory) as client:
        status, preview = await cut_preview(client, uid, c.tabbed, "30", T)
        assert status == 200
        form = hidden_fields(preview)
        async with env.factory() as s:  # the bot (or another tab) edits meanwhile
            if moved == "changed":
                await set_plan_price(s, uid, c.a50, 141_000, duration_days=30, tier=T)
            else:
                await clear_plan_price(s, uid, c.a100, duration_days=30, tier=T)
            await s.commit()
        after_edit = await snapshot(env.factory, uid)
        status, html = await post(client, uid, "cut", form)
    assert status == 409 and TEXT_STALE_RESELLER in html
    assert await snapshot(env.factory, uid) == after_edit


@pytest.mark.asyncio
async def test_a_price_outside_the_set_changing_does_not_stale_the_confirm(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        _, preview = await cut_preview(client, uid, c.tabbed, "30", T)
        form = hidden_fields(preview)
        await seed_prices(
            env.factory, uid, [(c.f10, 30, T, 50_000), (c.a50, 60, T, 150_000)]
        )  # another group, another tab
        status, _ = await post(client, uid, "cut", form)
    assert status == 200
    assert (await snapshot(env.factory, uid))["prices"] == _prices(
        (c.a50, 30, T, 126_000), (c.f10, 30, T, 50_000), (c.a50, 60, T, 150_000)
    )


@pytest.mark.asyncio
async def test_a_price_that_joined_the_set_makes_the_confirm_stale(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        _, preview = await cut_preview(client, uid, c.tabbed, "30", T)
        form = hidden_fields(preview)
        await seed_prices(env.factory, uid, [(c.a100, 30, T, 230_000)])  # now two cells would move
        after_edit = await snapshot(env.factory, uid)
        status, html = await post(client, uid, "cut", form)
    assert status == 409 and TEXT_STALE_RESELLER in html
    assert await snapshot(env.factory, uid) == after_edit, "the larger set was not applied"


@pytest.mark.asyncio
async def test_the_confirm_form_cannot_be_turned_into_another_cut(env) -> None:
    """The digest covers the percent and the set: a different percent, tab or group is stale."""
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, _pre_tabbed(c) + ((c.f50, 60, T, 240_000),))
    before = await snapshot(env.factory, uid)
    async with panel_client(env.factory) as client:
        _, preview = await cut_preview(client, uid, c.tabbed, "30", T, "10")
        form = hidden_fields(preview)
        for forged in (
            {**form, "pct": "50"},
            {**form, "pct": "11"},
            {**form, "d": "60"},
            {**form, "t": PREMIUM_TIER},
            {**form, "c": c.flat},
            {**form, "seen": "0" * 32},
            {k: v for k, v in form.items() if k != "seen"},
            {**form, "seen": ""},
        ):
            status, html = await post(client, uid, "cut", forged)
            assert status == 409 and TEXT_STALE_RESELLER in html, forged
            assert await snapshot(env.factory, uid) == before, forged


@pytest.mark.asyncio
async def test_a_cut_moves_only_cells_with_an_agreed_price_on_this_screens_plans(env) -> None:
    c, uid = env.cat, env.uid
    # one agreed cell in this group's 30-day tab; the other plan, the other tab, the other
    # tier and the other group's plan on the same 30-day tab all have an agreed price too
    # (or none): none of them may move, and no cell may be created from retail.
    await seed_prices(
        env.factory,
        uid,
        [
            (c.a50, 30, T, 140_000),
            (c.a50, 60, T, 250_000),
            (c.a50, 30, PREMIUM_TIER, 190_000),
            (c.f10, 30, T, 55_000),
        ],
    )
    async with panel_client(env.factory) as client:
        assert (await w_cut(client, uid, c.tabbed, 30, T, "10"))[0] == 200
    assert (await snapshot(env.factory, uid))["prices"] == _prices(
        (c.a50, 30, T, 126_000),
        (c.a50, 60, T, 250_000),
        (c.a50, 30, PREMIUM_TIER, 190_000),
        (c.f10, 30, T, 55_000),
    ), "a100 has no agreed price on this tab and gained none"
    # and the flat list's cut leaves the tabbed group's cell on the same duration alone
    await seed_prices(env.factory, uid, [(c.f50, 60, T, 240_000)])
    async with panel_client(env.factory) as client:
        assert (await w_cut(client, uid, c.flat, None, T, "10", flat=True))[0] == 200
    assert (await snapshot(env.factory, uid))["prices"] == _prices(
        (c.a50, 30, T, 126_000),
        (c.a50, 60, T, 250_000),
        (c.a50, 30, PREMIUM_TIER, 190_000),
        (c.f10, 30, T, 49_000),
        (c.f50, 60, T, 216_000),
    )


@pytest.mark.asyncio
async def test_an_undo_takes_back_exactly_the_cut_on_its_own_screen(env) -> None:
    c, uid = env.cat, env.uid
    base = [
        (c.a50, 30, T, 140_000),
        (c.a100, 30, T, 230_000),
        (c.f10, 30, T, 55_000),
        (c.f50, 60, T, 240_000),
    ]
    await seed_prices(env.factory, uid, base)
    async with panel_client(env.factory) as client:
        assert (await w_cut(client, uid, c.tabbed, 30, T, "10"))[0] == 200
        assert (await w_cut(client, uid, c.flat, None, T, "20", flat=True))[0] == 200
        assert (await snapshot(env.factory, uid))["prices"] == _prices(
            (c.a50, 30, T, 126_000),
            (c.a100, 30, T, 207_000),
            (c.f10, 30, T, 44_000),
            (c.f50, 60, T, 192_000),
        )
        # the tabbed screen shares its 30-day tab with the flat list, but not its plans: its
        # undo is its own 10 percent cut, though the flat list's was made later
        status, html = await w_undo(client, uid, c.tabbed, 30, T)
        assert status == 200 and as_page(texts.admin_reseller_undo_done.format(count="۲")) in html
        assert (await snapshot(env.factory, uid))["prices"] == _prices(
            (c.a50, 30, T, 140_000),
            (c.a100, 30, T, 230_000),
            (c.f10, 30, T, 44_000),
            (c.f50, 60, T, 192_000),
        )
        status, html = await w_undo(client, uid, c.flat, None, T, flat=True)
        assert status == 200 and as_page(texts.admin_reseller_undo_done.format(count="۲")) in html
    assert (await snapshot(env.factory, uid))["prices"] == _prices(*base)
    assert (await snapshot(env.factory, uid))["ops"] == [], "both snapshots were used up"


@pytest.mark.asyncio
async def test_a_second_undo_is_refused_in_the_bots_words(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.tabbed, 30, T, "10")
        html = await get_html(client, uid, c.tabbed, "30", T)
        form = form_by_action(html, "/undo")
        assert (await post(client, uid, "undo", form))[0] == 200
        after = await snapshot(env.factory, uid)
        status, page = await post(client, uid, "undo", form)
    assert status == 409 and as_page(LINE_NOTHING_TO_UNDO) in page
    assert await snapshot(env.factory, uid) == after
    assert after["prices"] == _prices((c.a50, 30, T, 140_000))


@pytest.mark.asyncio
async def test_an_undo_form_drawn_before_a_newer_cut_is_stale(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.tabbed, 30, T, "10")
        old_form = form_by_action(await get_html(client, uid, c.tabbed, "30", T), "/undo")
        await w_cut(client, uid, c.tabbed, 30, T, "10")  # a second cut replaces the snapshot
        after = await snapshot(env.factory, uid)
        status, page = await post(client, uid, "undo", old_form)
    assert status == 409 and TEXT_STALE_RESELLER in page
    assert await snapshot(env.factory, uid) == after


@pytest.mark.asyncio
async def test_a_flat_list_cut_across_two_durations_is_one_undo(env) -> None:
    c, uid = env.cat, env.uid
    base = [(c.f10, 30, T, 55_000), (c.f50, 60, T, 240_000)]
    await seed_prices(env.factory, uid, base)
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.flat, None, T, "10", flat=True)
        status, html = await w_undo(client, uid, c.flat, None, T, flat=True)
    assert status == 200 and as_page(texts.admin_reseller_undo_done.format(count="۲")) in html
    assert (await snapshot(env.factory, uid))["prices"] == _prices(*base)


# --------------------------------------------------------------------------- #
# Stale forms, forms with no token, and a form sent twice
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_price_form_from_before_another_edit_is_refused_and_nothing_is_merged(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        old_set, old_clear = price_fields(html, c.a50), clear_fields(html, c.a50)
        await seed_prices(env.factory, uid, [(c.a50, 30, T, 135_000)])  # the bot edits meanwhile
        after_edit = await snapshot(env.factory, uid)
        for suffix, form in (("price", {**old_set, "value": "100000"}), ("price/clear", old_clear)):
            status, page = await post(client, uid, suffix, form)
            assert status == 409 and TEXT_STALE_RESELLER in page, suffix
            assert await snapshot(env.factory, uid) == after_edit, suffix
    assert after_edit["prices"] == _prices((c.a50, 30, T, 135_000))


@pytest.mark.asyncio
async def test_a_form_drawn_while_there_was_no_price_cannot_overwrite_one_made_since(env) -> None:
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        fields = price_fields(html, c.a50)
        assert fields["seen"] == "none"
        await seed_prices(env.factory, uid, [(c.a50, 30, T, 135_000)])
        status, page = await post(client, uid, "price", {**fields, "value": "100000"})
    assert status == 409 and TEXT_STALE_RESELLER in page
    assert (await snapshot(env.factory, uid))["prices"] == _prices((c.a50, 30, T, 135_000))


@pytest.mark.asyncio
async def test_a_form_drawn_with_a_price_cannot_recreate_one_cleared_since(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 135_000)])
    async with panel_client(env.factory) as client:
        fields = price_fields(await get_html(client, uid, c.tabbed, "30", T), c.a50)
        async with env.factory() as s:
            await clear_plan_price(s, uid, c.a50, duration_days=30, tier=T)
            await s.commit()
        status, _ = await post(client, uid, "price", {**fields, "value": "100000"})
    assert status == 409
    assert (await snapshot(env.factory, uid))["prices"] == []


@pytest.mark.asyncio
async def test_a_rate_form_from_before_another_edit_is_refused(env) -> None:
    uid = env.uid
    async with panel_client(env.factory) as client:
        assert (await w_rate(client, uid, "gb", "1500"))[0] == 200
        html = await get_html(client, uid)
        old = {
            field: {**form_by_action(html, f"/rate/{field}"), "value": "7"}
            for field in ("gb", "day", "quota", "tprice")
        }
        async with env.factory() as s:  # the bot edits one figure meanwhile
            await set_rate(s, uid, price_per_day_toman=60)
            await s.commit()
        after_edit = await snapshot(env.factory, uid)
        for field, form in old.items():
            status, page = await post(client, uid, f"rate/{field}", form)
            assert status == 409 and TEXT_STALE_RESELLER in page, field
            assert await snapshot(env.factory, uid) == after_edit, field
    assert after_edit["rate"] == (1500, 60, 0, 0), "the bot's day rate is still there"


@pytest.mark.asyncio
async def test_a_form_with_no_token_is_refused_like_a_stale_one(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.tabbed, 30, T, "10")  # leaves an undo snapshot to ask for
    before = await snapshot(env.factory, uid)
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        sets = price_fields(html, c.a50)
        clears = clear_fields(html, c.a50)
        undo = form_by_action(html, "/undo")
        rate = form_by_action(html, "/rate/gb")
        _, preview = await cut_preview(client, uid, c.tabbed, "30", T)
        confirm = hidden_fields(preview)
        forms = [
            ("price", {**sets, "value": "100000"}),
            ("price/clear", clears),
            ("rate/gb", {**rate, "value": "9"}),
            ("undo", undo),
            ("cut", confirm),
        ]
        for suffix, form in forms:
            for blank in ({k: v for k, v in form.items() if k != "seen"}, {**form, "seen": ""}):
                status, page = await post(client, uid, suffix, blank)
                assert status == 409 and TEXT_STALE_RESELLER in page, (suffix, blank)
                assert await snapshot(env.factory, uid) == before, suffix


@pytest.mark.asyncio
async def test_the_same_clear_sent_twice_clears_once(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        form = clear_fields(await get_html(client, uid, c.tabbed, "30", T), c.a50)
        first = await post(client, uid, "price/clear", form)
        after_first = await snapshot(env.factory, uid)
        second = await post(client, uid, "price/clear", form)
    assert (first[0], second[0]) == (200, 409) and TEXT_STALE_RESELLER in second[1]
    assert await snapshot(env.factory, uid) == after_first


@pytest.mark.asyncio
async def test_the_same_price_sent_twice_is_one_write_and_the_second_form_is_stale(env) -> None:
    c, uid = env.cat, env.uid
    async with panel_client(env.factory) as client:
        fields = price_fields(await get_html(client, uid, c.tabbed, "30", T), c.a50)
        form = {**fields, "value": "120000"}
        first, second = await post(client, uid, "price", form), await post(
            client, uid, "price", form
        )
    assert (first[0], second[0]) == (200, 409)
    assert (await snapshot(env.factory, uid))["prices"] == _prices((c.a50, 30, T, 120_000))


@pytest.mark.asyncio
async def test_the_same_cut_confirm_sent_twice_cuts_once(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000), (c.a100, 30, T, 230_000)])
    async with panel_client(env.factory) as client:
        _, preview = await cut_preview(client, uid, c.tabbed, "30", T)
        form = hidden_fields(preview)
        first = await post(client, uid, "cut", form)
        after_first = await snapshot(env.factory, uid)
        second = await post(client, uid, "cut", form)
    assert (first[0], second[0]) == (200, 409) and TEXT_STALE_RESELLER in second[1]
    assert await snapshot(env.factory, uid) == after_first
    assert after_first["prices"] == _prices((c.a50, 30, T, 126_000), (c.a100, 30, T, 207_000))


@pytest.mark.asyncio
async def test_the_same_undo_sent_twice_undoes_once(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000)])
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.tabbed, 30, T, "10")
        form = form_by_action(await get_html(client, uid, c.tabbed, "30", T), "/undo")
        first = await post(client, uid, "undo", form)
        after_first = await snapshot(env.factory, uid)
        second = await post(client, uid, "undo", form)
    assert (first[0], second[0]) == (200, 409)
    assert await snapshot(env.factory, uid) == after_first


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["price", "clear", "rate", "cut", "undo"])
async def test_two_forms_sent_at_the_same_moment_apply_once(env, kind: str) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, [(c.a50, 30, T, 140_000), (c.a100, 30, T, 230_000)])
    if kind == "undo":
        async with panel_client(env.factory) as client:
            await w_cut(client, uid, c.tabbed, 30, T, "10")
    async with panel_client(env.factory) as client:
        html = await get_html(client, uid, c.tabbed, "30", T)
        if kind == "price":
            suffix, form = "price", {**price_fields(html, c.a50), "value": "120000"}
        elif kind == "clear":
            suffix, form = "price/clear", clear_fields(html, c.a50)
        elif kind == "rate":
            suffix, form = "rate/gb", {**form_by_action(html, "/rate/gb"), "value": "1500"}
        elif kind == "cut":
            _, preview = await cut_preview(client, uid, c.tabbed, "30", T)
            suffix, form = "cut", hidden_fields(preview)
        else:
            suffix, form = "undo", form_by_action(html, "/undo")
        results = await asyncio.gather(
            post(client, uid, suffix, form), post(client, uid, suffix, form)
        )
    assert sorted(status for status, _ in results) == [200, 409], kind
    rows = await snapshot(env.factory, uid)
    expected = {
        "price": _prices((c.a50, 30, T, 120_000), (c.a100, 30, T, 230_000)),
        "clear": _prices((c.a100, 30, T, 230_000)),
        "rate": _prices((c.a50, 30, T, 140_000), (c.a100, 30, T, 230_000)),
        "cut": _prices((c.a50, 30, T, 126_000), (c.a100, 30, T, 207_000)),
        "undo": _prices((c.a50, 30, T, 140_000), (c.a100, 30, T, 230_000)),
    }[kind]
    assert rows["prices"] == expected, "applied once, not twice"
    if kind == "rate":
        assert rows["rate"] == (1500, 0, 0, 0)
    if kind == "cut":
        assert len(rows["ops"]) == 1, "one undo snapshot"


# --------------------------------------------------------------------------- #
# What `updated_at` really is, since the form's token is its isoformat()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_row_token_has_microsecond_resolution_on_sqlite(env) -> None:
    """Measured, not assumed: two different writes in one second get different tokens.

    The token is ``updated_at.isoformat()``. SQLite keeps the column as text with the
    microseconds Python wrote (the default and ``onupdate`` are ``datetime.now(UTC)``), so
    the token only repeats if two writes land in the same microsecond. A write that
    changes nothing issues no UPDATE and so keeps its token, which is harmless: the row
    did not change.
    """
    from services.reseller_admin import cell_token, rate_token

    c, uid = env.cat, env.uid
    tokens: list[str] = []
    rate_tokens: list[str] = []
    for n in range(40):
        async with env.factory() as s:
            await set_plan_price(s, uid, c.a50, 100_000 + n * 1000, duration_days=30, tier=T)
            await set_rate(s, uid, price_per_gb_toman=1000 + n)
            await s.commit()
        async with env.factory() as s:
            tokens.append(await cell_token(s, uid, (c.a50, 30, T)))
            rate_tokens.append(await rate_token(s, uid))
    assert len(set(tokens)) == len(tokens) == 40, "every one of 40 rapid writes has its own token"
    assert len(set(rate_tokens)) == 40
    stamps = {t.split(".")[0] for t in tokens}
    assert len(stamps) < 40, "...although many of them fall in the same second"
    assert all("." in t for t in tokens + rate_tokens), "a fractional part is stored and returned"
    # rewriting the same value changes no row, so the token (and an old form) stays valid
    async with env.factory() as s:
        await set_plan_price(s, uid, c.a50, 100_000 + 39 * 1000, duration_days=30, tier=T)
        await s.commit()
        assert await cell_token(s, uid, (c.a50, 30, T)) == tokens[-1]


# --------------------------------------------------------------------------- #
# Who may reach these routes, and that looking never writes
# --------------------------------------------------------------------------- #

ROUTES = {
    ("GET", "/panel/resellers"),
    ("GET", "/panel/resellers/{user_id}"),
    ("POST", "/panel/resellers/{user_id}/price"),
    ("POST", "/panel/resellers/{user_id}/price/clear"),
    ("POST", "/panel/resellers/{user_id}/rate/{field}"),
    ("POST", "/panel/resellers/{user_id}/cut"),
    ("POST", "/panel/resellers/{user_id}/undo"),
}
CUSTOMER_TG, RESELLER_TG = 4301, 4302


def _reseller_routes(app: Any) -> set[tuple[str, str]]:
    return {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.resource is not None
        and r.resource.canonical.startswith("/panel/resellers")
        and r.method != "HEAD"
    }


def _concrete_path(canonical: str, uid: int) -> str:
    return canonical.replace("{user_id}", str(uid)).replace("{field}", "gb")


@pytest.mark.asyncio
async def test_there_are_exactly_these_reseller_routes_and_none_grants_the_role(env) -> None:
    """A route added later with no case here fails the build; none of them touches the role."""
    async with panel_client(env.factory) as client:
        assert _reseller_routes(client.app) == ROUTES
        before = await dump_db(env.factory)
        for tail in ("role", "grant", "revoke", "toggle", "reseller", "ban", "wallet", "delete"):
            for method in ("GET", "POST"):
                resp = await client.request(
                    method, f"/panel/resellers/{env.uid}/{tail}", data={"x": "1"}, headers=ADMIN
                )
                assert resp.status in (404, 405), (method, tail)
        for method in ("GET", "POST"):
            resp = await client.request(
                method, f"/panel/customers/{env.uid}/reseller", data={"x": "1"}, headers=ADMIN
            )
            assert resp.status in (404, 405)
    assert await dump_db(env.factory) == before
    async with env.factory() as s:
        assert (await s.get(User, env.uid)).is_reseller is True


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_only_an_admin_reaches_any_reseller_route(env, public: bool) -> None:
    from tests.unit.web_personal_helpers import make_person, personal_client

    await make_person(env.factory, CUSTOMER_TG, name="مشتری")
    await make_person(env.factory, RESELLER_TG, name="نماینده", reseller=True)
    before = await dump_db(env.factory)
    async with personal_client(env.factory, public=public) as client:
        for method, canonical in sorted(ROUTES):
            url = _concrete_path(canonical, env.uid)
            data = {"slug": env.cat.a50, "d": "30", "t": T, "value": "100000", "seen": "none"}
            anon = await client.request(method, url, data=data if method == "POST" else None)
            assert anon.status == 401, (method, canonical, "anonymous")
            for tg in (CUSTOMER_TG, RESELLER_TG, STRANGER):
                resp = await client.request(
                    method,
                    url,
                    data=data if method == "POST" else None,
                    headers={INIT_HEADER: init_data(tg)},
                )
                assert resp.status == 403, (method, canonical, tg, public)
            # a tampered signature is the login's refusal too
            forged = {INIT_HEADER: init_data(CUSTOMER_TG, tamper_user_id=ENV_ADMIN)}
            resp = await client.request(
                method, url, data=data if method == "POST" else None, headers=forged
            )
            assert resp.status in (401, 403), (method, canonical, "forged")
    assert await dump_db(env.factory) == before, "no refusal wrote anything"


@pytest.mark.asyncio
async def test_a_get_never_writes(env) -> None:
    c, uid = env.cat, env.uid
    await seed_prices(env.factory, uid, _pre_tabbed(c) + ((c.f50, 60, T, 240_000),))
    async with panel_client(env.factory) as client:
        await w_cut(client, uid, c.tabbed, 30, T, "10")  # leaves an undo to draw
    before = await dump_db(env.factory)
    urls = [
        "/panel/resellers",
        "/panel/resellers?o=0",
        "/panel/resellers?o=999",
        "/panel/resellers?o=abc",
        f"/panel/resellers/{uid}",
        f"/panel/resellers/{uid}?c={c.tabbed}&d=30&t={T}",
        f"/panel/resellers/{uid}?c={c.tabbed}&d=60&t={PREMIUM_TIER}",
        f"/panel/resellers/{uid}?c={c.flat}",
        f"/panel/resellers/{uid}?c=nope&d=zz&t=gold",
        f"/panel/resellers/{uid}?c={'x' * 2000}&d={'9' * 2000}&t=%00",
        f"/panel/resellers/{uid}/price?slug={c.a50}&d=30&t=economy&value=1&seen=none",
        f"/panel/resellers/{uid}/cut?c={c.tabbed}&d=30&t=economy&pct=10&confirm=1",
        f"/panel/resellers/{uid}/undo?c={c.tabbed}&d=30&t=economy&seen=1",
        f"/panel/resellers/{uid}/rate/gb?value=5",
        f"/panel/resellers/{uid}/price/clear?slug={c.a50}&d=30&t=economy",
        f"/panel/customers/{uid}",
    ]
    async with panel_client(env.factory) as client:
        for url in urls:
            resp = await client.get(url, headers=ADMIN)
            assert resp.status in (200, 404, 405), (url, resp.status)
    assert await dump_db(env.factory) == before


@pytest.mark.asyncio
async def test_any_admin_may_write_a_store_owner_is_not_needed(env) -> None:
    """The bot's reseller controls ask only for an admin (`_deny_if_not_admin`), so the page does too."""
    settings = panel_settings()
    assert str(ENV_ADMIN) not in str(settings.store_owner_telegram_ids)
    async with panel_client(env.factory, settings) as client:
        status, _ = await web_set_price(client, env, env.cat.a50, "120000", d="30", t=T)
        assert status == 200
        assert (await w_rate(client, env.uid, "gb", "1500"))[0] == 200
    assert (await snapshot(env.factory, env.uid))["rate"] == (1500, 0, 0, 0)


@pytest.mark.asyncio
async def test_the_cut_prompt_says_write_on_the_web_and_send_in_the_bot(env) -> None:
    c = env.cat
    await seed_prices(env.factory, env.uid, [(c.a50, 30, T, 110_000), (c.f50, 60, T, 200_000)])
    async with panel_client(env.factory) as client:
        tabbed = await get_html(client, env.uid, c.tabbed, "30", T)
        flat = await get_html(client, env.uid, c.flat)
    for bot_key, web_key, html in (
        ("admin_reseller_pct_prompt", "web_reseller_pct_prompt", tabbed),
        ("admin_reseller_pct_prompt_flat", "web_reseller_pct_prompt_flat", flat),
    ):
        assert "بفرست" in getattr(texts, bot_key)
        assert getattr(texts, web_key) in html and "بفرست" not in html
        assert "بنویس" in getattr(texts, web_key)
    assert texts.web_reseller_pct_prompt == (
        "چند درصد از قیمت‌های نمایندهٔ همین تب کم شود؟ یک عدد بین ۱ تا ۹۰ بنویس."
    )


@pytest.mark.asyncio
async def test_the_reseller_sheet_cell_without_a_price_has_no_dash(env) -> None:
    """A cell without an agreed price reads exactly «نماینده: تعیین نشده» and has no «—» (Piece A2)."""
    c = env.cat
    async with panel_client(env.factory) as client:
        html = await get_html(client, env.uid, c.tabbed, "30", T)
    block = cell_block(html, c.a100)
    head = block.split('<div class="rs-head">')[1].split("</div>")[0]
    assert texts.web_reseller_cell_none in head
    assert '<span class="num">نماینده: تعیین نشده</span>' in head
    assert "—" not in block
    assert (
        head.count("نماینده") == 1
    ), "the word «نماینده» appears once, not twice in the cell header"
