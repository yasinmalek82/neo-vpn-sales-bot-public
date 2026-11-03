"""«🏷 کدهای تخفیف» on the Mini App (step 4 of the panel plan).

What these defend, each with a test that goes red when the real line is broken:

* a web create, toggle or delete leaves the same document, the same «تغییرات فروشگاه» row
  and the same version the bot's wizard and buttons leave for the same input (parity,
  against the bot's real handlers on a second database);
* a code made on the web counts its uses from its creation (``counted_from``), and a code
  deleted and created again starts from zero; the page shows the number the checks count;
* a code switched off on the web is refused by the real pricing function, and switched on
  again is accepted; both stored shapes render and survive an edit;
* a bad percent, limit, expiry or code is refused with the bot's line and writes nothing;
* every POST under ``/panel/promo`` obeys one guard (login, admin, store owner, current
  store version, GET writes nothing, a double submit applies once), and a route added later
  with no case here fails the build;
* a code containing HTML is escaped, on the list, in the form and in the delete question.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Awaitable, Callable
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
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, Plan, PromoRedemption, User
from database.models.plan import DataLimitResetStrategy
from database.models.store import StoreVersion
from services.pricing import PlanType, calculate_price
from services.promo_admin import TEXT_PROMO_MISSING
from services.promo_redemptions import count_live_redemptions, live_usage_by_code
from services.store_admin import commit_and_activate, get_catalog_view, get_or_create_draft
from services.store_config import load_pricing_config_async, seed_store_from_pricing_json
from services.store_edit import TEXT_STALE, published_store_version
from services.store_strategy import delete_draft_promo, list_promo_codes, list_store_changes
from services.ux_format import to_persian_digits
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
OLD_STAMP = "2026-01-05T08:00:00+00:00"
EXPIRES = "2027-01-01T09:30:00+00:00"
SEED_PROMOS: dict[str, Any] = {
    "BARE": "10",
    "RULED": {
        "percent": 20,
        "active": True,
        "first_purchase_only": True,
        "per_user_limit": 1,
        "total_limit": 5,
        "expires_at": EXPIRES,
        "counted_from": OLD_STAMP,
    },
    "OFFC": {"percent": 15, "active": False},
}
HTML_CODE = '"><script>alert(1)</script>'


def _owner_settings() -> Any:
    return panel_settings(STORE_OWNER_TELEGRAM_IDS=str(ENV_ADMIN))


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    """The shipped pricing file (WELCOME10, object shape), plus a bare code, a ruled code and an off code."""
    async with factory() as s:
        await seed_store_from_pricing_json(s, Path("config/pricing.json"))
        await s.commit()
        draft = await get_or_create_draft(s)
        doc = copy.deepcopy(draft.pricing_document)
        doc["promo_codes"] = {**doc["promo_codes"], **SEED_PROMOS}
        draft.pricing_document = doc
        await commit_and_activate(s, notes="seed")


@pytest_asyncio.fixture
async def seeded(two_sessions: Factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory


async def _promos(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    async with factory() as s:
        view = await get_catalog_view(s)
        assert view.version is not None
        return dict(view.version.pricing_document["promo_codes"])


async def _log(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as s:
        return [e.description for e in await list_store_changes(s)]


async def _version(factory: async_sessionmaker[AsyncSession]) -> int | None:
    async with factory() as s:
        return await published_store_version(s)


async def _version_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(StoreVersion)) or 0)


def _normal(entry: Any) -> Any:
    """A stored entry with the two clock-dependent fields turned into comparable facts."""
    if not isinstance(entry, dict):
        return entry
    now = datetime.now(UTC)
    out = dict(entry)
    if "counted_from" in out:
        age = now - datetime.fromisoformat(out["counted_from"])
        out["counted_from"] = (
            "stamped now" if timedelta(0) <= age < timedelta(minutes=5) else out["counted_from"]
        )
    if "expires_at" in out:
        out["expires_at"] = round(
            (datetime.fromisoformat(out["expires_at"]) - now).total_seconds() / 86400
        )
    return out


async def _snapshot(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    return {
        "promos": {code: _normal(entry) for code, entry in (await _promos(factory)).items()},
        "log": await _log(factory),
        "version": await _version(factory),
    }


async def _page(client: TestClient, headers: dict[str, str] = OWNER) -> str:
    resp = await client.get("/panel/promo", headers=headers)
    assert resp.status == 200
    return await resp.text()


async def _post(
    client: TestClient,
    tail: str,
    form: dict[str, str],
    *,
    version: int | None,
    headers: dict[str, str] = OWNER,
) -> tuple[int, str]:
    data = dict(form)
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(f"/panel/promo/{tail}", data=data, headers=headers)
    return resp.status, await resp.text()


def _row(html: str, code: str) -> str:
    """The HTML of one code's card."""
    start = html.index(f'<span class="pr-code lat">{code}</span>')
    nxt = html.find('<div class="promo', start)
    return html[start : nxt if nxt != -1 else len(html)]


# --------------------------------------------------------------------------- #
# The bot's own wizard and buttons, driven for parity
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


BotDriver = Callable[[AsyncSession], Awaitable[None]]


def _bot_create(
    code: str,
    percent: str,
    *,
    first: bool | None = None,
    per_user: str | None = None,
    total: str | None = None,
    days: str | None = None,
) -> BotDriver:
    """The wizard as the owner walks it: «کد درصد», then each question answered or skipped."""

    async def drive(s: AsyncSession) -> None:
        fsm, settings = _fsm(), _bot_settings()
        await fsm.set_state(AdminStates.store_waiting_promo)
        await ha.store_promo_fsm(_typed(f"{code} {percent}"), fsm, settings, None)
        answer = {True: "y", False: "n", None: "s"}[first]
        await ha.store_promo_wizard_button(
            _callback(f"{StoreCB.PROMO_Q}{answer}"), fsm, settings, None, s
        )

        async def step(typed: str | None, handler: Any) -> None:
            if typed is None:
                await ha.store_promo_wizard_button(
                    _callback(f"{StoreCB.PROMO_Q}s"), fsm, settings, None, s
                )
            else:
                await handler(_typed(typed), fsm, settings, None, s)

        if not first:
            await step(per_user, ha.store_promo_user_limit)
        await step(total, ha.store_promo_total_limit)
        await step(days, ha.store_promo_days)

    return drive


def _bot_tap(handler_name: str, prefix: str, code: str) -> BotDriver:
    async def drive(s: AsyncSession) -> None:
        await getattr(ha, handler_name)(_callback(f"{prefix}{code}"), _bot_settings(), None, s)

    return drive


class Case:
    def __init__(self, tail: str, form: dict[str, str], bot: BotDriver, canonical: str) -> None:
        self.tail, self.form, self.bot, self.canonical = tail, form, bot, canonical

    def __repr__(self) -> str:
        return f"{self.tail}:{sorted(self.form.items())}"


_ADD = "/panel/promo/add"
_TOGGLE = "/panel/promo/c/{code}/toggle"
_DELETE = "/panel/promo/c/{code}/delete"
CASES = [
    Case(
        "add",
        {"code": "full", "percent": "15", "per_user": "2", "total": "50", "days": "30"},
        _bot_create("full", "15", first=False, per_user="2", total="50", days="30"),
        _ADD,
    ),
    Case(
        "add",
        {"code": "once", "percent": "20%", "first": "1", "total": "10"},
        _bot_create("once", "20%", first=True, total="10"),
        _ADD,
    ),
    Case("add", {"code": "plain", "percent": "5"}, _bot_create("plain", "5"), _ADD),
    Case(
        "add",
        {"code": "welcome10", "percent": "12", "total": "۳", "days": "۷"},
        _bot_create("welcome10", "12", total="۳", days="۷"),
        _ADD,
    ),
    Case(
        "add",
        {"code": "BARE", "percent": "12.5", "per_user": "3"},
        _bot_create("BARE", "12.5", per_user="3"),
        _ADD,
    ),
    Case(
        "add",
        {"code": "RULED", "percent": "20"},
        _bot_create("RULED", "20"),
        _ADD,
    ),
    Case("c/BARE/toggle", {}, _bot_tap("store_promo_toggle", StoreCB.PROMO_TG, "BARE"), _TOGGLE),
    Case(
        "c/WELCOME10/toggle",
        {},
        _bot_tap("store_promo_toggle", StoreCB.PROMO_TG, "WELCOME10"),
        _TOGGLE,
    ),
    Case("c/OFFC/toggle", {}, _bot_tap("store_promo_toggle", StoreCB.PROMO_TG, "OFFC"), _TOGGLE),
    Case("c/RULED/toggle", {}, _bot_tap("store_promo_toggle", StoreCB.PROMO_TG, "RULED"), _TOGGLE),
    Case(
        "c/RULED/delete",
        {"confirm": "1"},
        _bot_tap("store_promo_del", StoreCB.PROMO_DEL_OK, "RULED"),
        _DELETE,
    ),
    Case(
        "c/BARE/delete",
        {"confirm": "1"},
        _bot_tap("store_promo_del", StoreCB.PROMO_DEL_OK, "BARE"),
        _DELETE,
    ),
]

# The row «↩️ تغییرات» reads back to the owner, written out here and not imported.
EXPECTED_NOTE = {
    "add": "کد تخفیف ثبت شد",
    "toggle": "کد تخفیف روشن/خاموش شد",
    "delete": "کد تخفیف حذف شد",
}


def _kind(case: Case) -> str:
    return "add" if case.tail == "add" else case.tail.rpartition("/")[2]


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
async def test_a_web_write_leaves_the_document_and_log_the_bots_control_leaves(
    twin, case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, bot_factory = twin
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)

    async with bot_factory() as s:
        await case.bot(s)

    before = await _snapshot(web_factory)
    async with panel_client(web_factory, _owner_settings()) as client:
        status, html = await _post(
            client, case.tail, case.form, version=await _version(web_factory)
        )
    assert status == 200, html
    after = await _snapshot(web_factory)
    assert (
        after != before
    ), "the write must have changed something for the comparison to mean anything"
    assert after == await _snapshot(bot_factory)
    assert after["log"][0] == EXPECTED_NOTE[_kind(case)]  # type: ignore[index]
    assert texts.admin_store_saved in html


# --------------------------------------------------------------------------- #
# The list: the bot's rules and usage, both stored shapes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_list_shows_every_code_with_its_percent_state_rules_and_usage(seeded) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        html = await _page(client)
    assert '<p class="sub">۴ کد</p>' in html
    assert "🏷 کدهای تخفیف" not in html
    bare, ruled, off = _row(html, "BARE"), _row(html, "RULED"), _row(html, "OFFC")
    assert "۱۰٪" in bare and "✅ فعال" in bare and texts.admin_promo_rules_none in bare
    assert texts.admin_promo_usage_unlimited.format(used="۰") in bare
    assert "۲۰٪" in ruled and texts.admin_promo_rule_first_only in ruled
    assert texts.admin_promo_rule_expires.format(date="۱۴۰۵/۱۰/۱۱ ۱۳:۰۰") in ruled
    assert texts.admin_promo_usage.format(used="۰", limit="۵") in ruled
    assert "❌ غیرفعال" in off and "۱۵٪" in off
    assert "WELCOME10" in _row(html, "WELCOME10") and "۱۰٪" in _row(html, "WELCOME10")


@pytest.mark.asyncio
async def test_the_live_usage_on_the_page_is_the_number_the_checks_count(seeded) -> None:
    """Two uses after ``counted_from``, one before it, one on a dead order: the page says two."""
    async with seeded() as s:
        user = User(telegram_id=9951, referral_code="P9951")
        plan = Plan(
            name="p",
            description="t",
            data_limit_gb=10,
            duration_days=30,
            price_toman=1000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=0,
            is_active=True,
        )
        s.add_all([user, plan])
        await s.flush()
        stamp = datetime.fromisoformat(OLD_STAMP)
        for status, created in (
            (OrderStatus.ACTIVE, stamp - timedelta(days=1)),
            (OrderStatus.ACTIVE, stamp + timedelta(days=1)),
            (OrderStatus.ACTIVE, stamp + timedelta(days=2)),
            (OrderStatus.REJECTED, stamp + timedelta(days=3)),
        ):
            order = Order(user_id=user.id, plan_id=plan.id, status=status, price_toman=1000)
            s.add(order)
            await s.flush()
            s.add(
                PromoRedemption(
                    code="RULED", user_id=user.id, order_id=order.id, created_at=created
                )
            )
        await s.commit()
        view = await get_catalog_view(s)
        assert view.version is not None
        rows = list_promo_codes(view.version.pricing_document)
        usage = await live_usage_by_code(s, since={r["code"]: r["counted_from"] for r in rows})
    assert usage["RULED"] == 2
    async with panel_client(seeded, _owner_settings()) as client:
        html = await _page(client)
    assert texts.admin_promo_usage.format(
        used=to_persian_digits(usage["RULED"]), limit="۵"
    ) in _row(html, "RULED")


@pytest.mark.asyncio
async def test_looking_writes_nothing(seeded) -> None:
    rows, log = await _version_rows(seeded), await _log(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        await _page(client)
    assert await _version_rows(seeded) == rows and await _log(seeded) == log


@pytest.mark.asyncio
async def test_an_admin_who_is_not_a_store_owner_reads_and_has_no_control(seeded) -> None:
    async with panel_client(seeded, panel_settings()) as client:
        html = await _page(client)
    assert texts.admin_store_promo_ops_hint in html
    assert "—" not in html.split('class="page-head"')[1].split("</div>")[0]
    assert "hx-post" not in html and 'name="version"' not in html
    assert "BARE" in html


@pytest.mark.asyncio
async def test_no_catalog_says_so(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory, _owner_settings()) as client:
        html = await _page(client)
    assert texts.admin_store_no_catalog in html


# --------------------------------------------------------------------------- #
# counted_from: the same create function on both paths
# --------------------------------------------------------------------------- #


async def _use(factory: async_sessionmaker[AsyncSession], code: str, times: int) -> None:
    async with factory() as s:
        user = User(telegram_id=9100 + times, referral_code=f"U{code}{times}")
        plan = Plan(
            name=f"p{code}{times}",
            description="t",
            data_limit_gb=10,
            duration_days=30,
            price_toman=1000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=0,
            is_active=True,
        )
        s.add_all([user, plan])
        await s.flush()
        for _ in range(times):
            order = Order(
                user_id=user.id, plan_id=plan.id, status=OrderStatus.ACTIVE, price_toman=1000
            )
            s.add(order)
            await s.flush()
            s.add(PromoRedemption(code=code, user_id=user.id, order_id=order.id))
        await s.commit()


@pytest.mark.asyncio
async def test_a_code_made_on_the_web_counts_from_the_moment_it_was_created(seeded) -> None:
    before = datetime.now(UTC)
    async with panel_client(seeded, _owner_settings()) as client:
        status, _ = await _post(
            client, "add", {"code": "fresh", "percent": "10"}, version=await _version(seeded)
        )
    after = datetime.now(UTC)
    assert status == 200
    entry = (await _promos(seeded))["FRESH"]
    stamp = datetime.fromisoformat(entry["counted_from"])
    assert before <= stamp <= after


@pytest.mark.asyncio
async def test_a_code_deleted_and_created_again_on_the_web_starts_from_zero(seeded) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        await _post(
            client,
            "add",
            {"code": "again", "percent": "10", "total": "2"},
            version=await _version(seeded),
        )
        await asyncio.sleep(0.01)
        await _use(seeded, "AGAIN", 2)
        html = await _page(client)
        assert texts.admin_promo_usage.format(used="۲", limit="۲") in _row(html, "AGAIN")
        stale_stamp = (await _promos(seeded))["AGAIN"]["counted_from"]

        await _post(client, "c/AGAIN/delete", {"confirm": "1"}, version=await _version(seeded))
        await asyncio.sleep(0.01)
        status, html = await _post(
            client,
            "add",
            {"code": "again", "percent": "10", "total": "2"},
            version=await _version(seeded),
        )
    assert status == 200
    entry = (await _promos(seeded))["AGAIN"]
    assert entry["counted_from"] > stale_stamp, "a new stamp, not the deleted code's"
    assert texts.admin_promo_usage.format(used="۰", limit="۲") in _row(html, "AGAIN")
    async with seeded() as s:
        since = datetime.fromisoformat(entry["counted_from"])
        assert await count_live_redemptions(s, "AGAIN", counted_from=since) == 0
        assert await count_live_redemptions(s, "AGAIN") == 2, "the old uses are still in the table"


@pytest.mark.asyncio
async def test_replacing_a_code_on_the_web_keeps_its_stamp_and_its_count(seeded) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        status, html = await _post(
            client,
            "add",
            {"code": "ruled", "percent": "25", "total": "9"},
            version=await _version(seeded),
        )
    assert status == 200
    entry = (await _promos(seeded))["RULED"]
    assert entry["counted_from"] == OLD_STAMP
    assert entry["percent"] == 25.0 and entry["total_limit"] == 9
    assert "first_purchase_only" not in entry, "the new answers replace the old rules"


# --------------------------------------------------------------------------- #
# Checkout stays the one place that refuses an inactive code
# --------------------------------------------------------------------------- #


async def _checkout(factory: async_sessionmaker[AsyncSession], code: str) -> Any:
    async with factory() as s:
        cfg = await load_pricing_config_async(s, settings=_owner_settings())
    return calculate_price(
        cfg, plan_type=PlanType.VOLUME, volume_gb=50, duration_days=30, promo_code=code
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["WELCOME10", "BARE", "RULED"])
async def test_a_code_switched_off_on_the_web_is_refused_at_checkout_and_on_again_is_accepted(
    seeded, code: str
) -> None:
    assert (await _checkout(seeded, code)).discount_amount > 0
    async with panel_client(seeded, _owner_settings()) as client:
        await _post(client, f"c/{code}/toggle", {}, version=await _version(seeded))
        with pytest.raises(ValueError, match="invalid_promo_code"):
            await _checkout(seeded, code)
        html = await _page(client)
        assert "❌ غیرفعال" in _row(html, code)
        await _post(client, f"c/{code}/toggle", {}, version=await _version(seeded))
    assert (await _checkout(seeded, code)).discount_amount > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["BARE", "WELCOME10"])
async def test_both_stored_shapes_survive_a_web_edit(seeded, code: str) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        status, _ = await _post(
            client,
            "add",
            {"code": code, "percent": "12", "total": "4"},
            version=await _version(seeded),
        )
        assert status == 200
        html = await _page(client)
    entry = (await _promos(seeded))[code]
    assert entry["percent"] == 12.0 and entry["active"] is True and entry["total_limit"] == 4
    assert "۱۲٪" in _row(html, code)
    priced = await _checkout(seeded, code)
    assert priced.discount_amount == int(priced.original_price * 0.12)


@pytest.mark.asyncio
async def test_the_toggle_writes_what_the_bots_toggle_writes_and_nothing_else(seeded) -> None:
    before = await _promos(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        await _post(client, "c/RULED/toggle", {}, version=await _version(seeded))
        await _post(client, "c/BARE/toggle", {}, version=await _version(seeded))
    after = await _promos(seeded)
    assert after["RULED"] == {
        **before["RULED"],
        "active": False,
    }, "every rule key and the stamp stay"
    assert after["BARE"] == {"percent": "10", "active": False}
    assert {k: v for k, v in after.items() if k not in ("RULED", "BARE")} == {
        k: v for k, v in before.items() if k not in ("RULED", "BARE")
    }


# --------------------------------------------------------------------------- #
# Validation: the bot's lines, nothing written
# --------------------------------------------------------------------------- #

INVALID_PERCENT = "درصد تخفیف نامعتبر است. یک عدد بین ۱ تا ۱۰۰ بفرستید."
RANGE = "درصد تخفیف باید بین ۰ و ۱۰۰ باشد."
CODE_RULE = "کد تخفیف باید ۲ تا ۳۲ حرف انگلیسی/عدد/زیرخط باشد"
BAD = [
    ({"code": "abc", "percent": "0"}, RANGE),
    ({"code": "abc", "percent": "101"}, RANGE),
    ({"code": "abc", "percent": "-5"}, RANGE),
    ({"code": "abc", "percent": "nan"}, RANGE),
    ({"code": "abc", "percent": "inf"}, RANGE),
    ({"code": "abc", "percent": NINES}, RANGE),
    ({"code": "abc", "percent": "abc"}, INVALID_PERCENT),
    ({"code": "abc", "percent": ""}, texts.admin_store_invalid_input),
    ({"code": "", "percent": "10"}, texts.admin_store_invalid_input),
    ({"code": "   ", "percent": "10"}, texts.admin_store_invalid_input),
    ({"code": "a", "percent": "10"}, CODE_RULE),
    ({"code": "bad code", "percent": "10"}, CODE_RULE),
    ({"code": "X" * 33, "percent": "10"}, CODE_RULE),
    ({"code": HTML_CODE, "percent": "10"}, CODE_RULE),
    ({"code": "کد", "percent": "10"}, CODE_RULE),
    ({"code": "abc", "percent": "10", "per_user": "x"}, texts.admin_promo_invalid_number),
    ({"code": "abc", "percent": "10", "per_user": "-1"}, texts.admin_promo_invalid_number),
    ({"code": "abc", "percent": "10", "per_user": "1.5"}, texts.admin_promo_invalid_number),
    ({"code": "abc", "percent": "10", "total": "²"}, texts.admin_promo_invalid_number),
    ({"code": "abc", "percent": "10", "total": "1000001"}, "عددی تا ۱٬۰۰۰٬۰۰۰"),
    ({"code": "abc", "percent": "10", "total": NINES}, "عددی تا ۱٬۰۰۰٬۰۰۰"),
    ({"code": "abc", "percent": "10", "days": "abc"}, texts.admin_promo_invalid_number),
    ({"code": "abc", "percent": "10", "days": "3651"}, "عددی تا ۳٬۶۵۰"),
    ({"code": "abc", "percent": "10", "days": NINES}, "عددی تا ۳٬۶۵۰"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("form", "line"), BAD, ids=[f"{f}" for f, _ in BAD])
async def test_a_bad_input_is_refused_with_the_bots_line_and_nothing_is_written(
    seeded, form: dict[str, str], line: str
) -> None:
    before, rows = await _snapshot(seeded), await _version_rows(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        status, html = await _post(client, "add", form, version=await _version(seeded))
    assert status == 400, html
    assert line.replace("٬", "") in html.replace("٬", "") or line in html, (line, html[-600:])
    assert await _snapshot(seeded) == before
    assert await _version_rows(seeded) == rows, "not even a draft is left behind"


@pytest.mark.asyncio
async def test_the_bot_refuses_the_same_percents(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """The page and the bot's wizard share one validation: 'nan' is refused by both."""
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    fsm = _fsm()
    await fsm.set_state(AdminStates.store_waiting_promo)
    typed = _typed("ABC nan")
    await ha.store_promo_fsm(typed, fsm, _bot_settings(), None)
    assert RANGE in typed.answer.await_args.args[0]
    assert await fsm.get_state() == AdminStates.store_waiting_promo.state


@pytest.mark.asyncio
async def test_a_typed_number_the_driver_cannot_read_is_a_line_not_a_crash(seeded) -> None:
    """«²» passes str.isdigit() and then int() raises; the bot's old prompt let that escape."""
    fsm = _fsm()
    await fsm.set_state(AdminStates.store_promo_total_limit)
    typed = _typed("²")
    async with seeded() as s:
        await ha.store_promo_total_limit(typed, fsm, _bot_settings(), None, s)
    assert texts.admin_promo_invalid_number in typed.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_an_error_keeps_what_was_typed_and_escapes_it(seeded) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        status, html = await _post(
            client,
            "add",
            {"code": HTML_CODE, "percent": "10"},
            version=await _version(seeded),
        )
    assert status == 400
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.asyncio
async def test_a_code_with_html_in_the_document_is_escaped_on_the_list_and_the_question(
    seeded,
) -> None:
    async with seeded() as s:
        draft = await get_or_create_draft(s)
        doc = copy.deepcopy(draft.pricing_document)
        doc["promo_codes"] = {**doc["promo_codes"], "<B>X</B>": "10"}
        draft.pricing_document = doc
        await commit_and_activate(s, notes="hand edit")
    async with panel_client(seeded, _owner_settings()) as client:
        html = await _page(client)
        assert "<B>X</B>" not in html and "&lt;B&gt;X&lt;/B&gt;" in html
        status, ask = await _post(
            client, "c/%3CB%3EX%3C%2FB%3E/delete", {}, version=await _version(seeded)
        )
    assert status == 200
    assert "<B>X</B>" not in ask and "&lt;B&gt;X&lt;/B&gt;" in ask


@pytest.mark.asyncio
async def test_a_code_the_list_does_not_hold_is_the_bots_not_found_line(seeded) -> None:
    before = await _snapshot(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        for tail, form in (
            ("c/NOPE/toggle", {}),
            ("c/NOPE/delete", {"confirm": "1"}),
            ("c/NOPE/delete", {}),
        ):
            status, html = await _post(client, tail, form, version=await _version(seeded))
            assert status == 400 and TEXT_PROMO_MISSING in html, tail
    assert await _snapshot(seeded) == before
    async with seeded() as s:
        with pytest.raises(Exception) as raised:
            await delete_draft_promo(s, "NOPE")
    assert str(raised.value) == TEXT_PROMO_MISSING, "the page's line is the service's line"


@pytest.mark.asyncio
async def test_a_duplicate_code_replaces_the_code_as_the_bots_wizard_does(seeded) -> None:
    async with panel_client(seeded, _owner_settings()) as client:
        status, _ = await _post(
            client, "add", {"code": "offc", "percent": "30"}, version=await _version(seeded)
        )
    assert status == 200
    entry = (await _promos(seeded))["OFFC"]
    assert entry["percent"] == 30.0 and entry["active"] is True, "re-saving also switches it on"


# --------------------------------------------------------------------------- #
# Deleting asks first, with the bot's sentence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deleting_asks_first_with_the_bots_sentence_and_removes_nothing(seeded) -> None:
    before, rows = await _snapshot(seeded), await _version_rows(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        status, html = await _post(client, "c/BARE/delete", {}, version=await _version(seeded))
        assert status == 200
        assert texts.admin_store_promo_del_confirm.format(code="BARE") in html
        assert 'name="confirm" value="1"' in html
        assert await _snapshot(seeded) == before and await _version_rows(seeded) == rows
        status, page = await _post(
            client, "c/BARE/delete", {"confirm": "1"}, version=await _version(seeded)
        )
    assert status == 200 and "BARE" not in (await _promos(seeded))
    assert (await _log(seeded))[0] == "کد تخفیف حذف شد"


# --------------------------------------------------------------------------- #
# One guard for every write route
# --------------------------------------------------------------------------- #


def _concrete(case: Case) -> str:
    return f"/panel/promo/{case.tail}"


@pytest.mark.asyncio
async def test_every_post_route_under_the_promo_page_has_a_guard_case(seeded) -> None:
    """A write route added later with no case in CASES fails here."""
    covered = {case.canonical for case in CASES}
    async with panel_client(seeded, _owner_settings()) as client:
        posts = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "POST" and r.resource and r.resource.canonical.startswith("/panel/promo")
        }
    assert posts, "the promo page has write routes"
    assert posts == covered, f"uncovered: {posts - covered}, unknown: {covered - posts}"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_write_needs_login_admin_owner_and_the_current_version(seeded, case: Case) -> None:
    url = _concrete(case)
    version = await _version(seeded)
    form = {**case.form, "version": str(version)}
    before, rows = await _snapshot(seeded), await _version_rows(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        assert (await client.post(url, data=form)).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (await client.post(url, data=form, headers=stranger)).status == 403
        # GET never writes (and the write routes have no GET)
        assert (await client.get(url, headers=OWNER)).status == 405
        assert (await client.get(f"{url}?version={version}", headers=OWNER)).status == 405
        # a form drawn from an older version, and one with no version at all
        resp = await client.post(
            url, data={**case.form, "version": str((version or 0) - 1)}, headers=OWNER
        )
        assert resp.status == 409 and TEXT_STALE in await resp.text()
        resp = await client.post(url, data=case.form, headers=OWNER)
        assert resp.status == 409, "no version at all is as stale as a wrong one"
    assert await _snapshot(seeded) == before
    assert await _version_rows(seeded) == rows

    # an admin who is not a store owner reads but cannot write
    async with panel_client(seeded, panel_settings()) as client:
        resp = await client.post(url, data=form, headers=OWNER)
        assert resp.status == 403 and texts.admin_store_owner_only in await resp.text()
    assert await _snapshot(seeded) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=repr)
async def test_a_form_sent_twice_applies_once(seeded, case: Case) -> None:
    url = _concrete(case)
    version = await _version(seeded)
    log_before = len(await _log(seeded))
    form = {**case.form, "version": str(version)}
    async with panel_client(seeded, _owner_settings()) as client:
        first = await client.post(url, data=form, headers=OWNER)
        after_first = await _snapshot(seeded)
        second = await client.post(url, data=form, headers=OWNER)
        html = await second.text()
    assert (first.status, second.status) == (200, 409)
    assert TEXT_STALE in html
    assert await _snapshot(seeded) == after_first, "the second form changed nothing"
    assert len(await _log(seeded)) == log_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [CASES[0], CASES[6], CASES[10]], ids=repr)
async def test_two_forms_at_the_same_moment_apply_once(seeded, case: Case) -> None:
    url = _concrete(case)
    form = {**case.form, "version": str(await _version(seeded))}
    log_before = len(await _log(seeded))
    async with panel_client(seeded, _owner_settings()) as client:
        results = await asyncio.gather(
            client.post(url, data=form, headers=OWNER), client.post(url, data=form, headers=OWNER)
        )
    assert sorted(r.status for r in results) == [200, 409]
    assert len(await _log(seeded)) == log_before + 1


@pytest.mark.asyncio
async def test_a_toggle_sent_twice_never_flips_the_code_back(seeded) -> None:
    version = await _version(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        for _ in range(2):
            await client.post(
                "/panel/promo/c/BARE/toggle", data={"version": str(version)}, headers=OWNER
            )
    assert (await _promos(seeded))["BARE"]["active"] is False


@pytest.mark.asyncio
async def test_a_form_from_before_the_bot_edited_the_catalog_is_refused(seeded) -> None:
    old = await _version(seeded)
    async with seeded() as s:
        draft = await get_or_create_draft(s)
        draft.sales_rules = {**dict(draft.sales_rules or {}), "x": 1}
        await commit_and_activate(s, notes="bot edit")
    before = await _promos(seeded)
    async with panel_client(seeded, _owner_settings()) as client:
        status, html = await _post(client, "c/BARE/toggle", {}, version=old)
    assert status == 409 and TEXT_STALE in html
    assert await _promos(seeded) == before


@pytest.mark.asyncio
async def test_the_form_asks_to_write_a_number_where_the_bot_asks_to_send_one(seeded) -> None:
    """The web twins say «بنویس»; the bot's lines keep «بفرست» (a chat sends, a form types)."""
    async with panel_client(seeded, _owner_settings()) as client:
        html = await _page(client)
    for bot_key, web_key in (
        ("admin_promo_ask_user_limit", "web_promo_ask_user_limit"),
        ("admin_promo_ask_total_limit", "web_promo_ask_total_limit"),
        ("admin_promo_ask_days", "web_promo_ask_days"),
    ):
        assert "بفرست" in getattr(texts, bot_key)
        assert "بفرست" not in getattr(texts, web_key)
        assert getattr(texts, web_key).split("\n")[0] in html
        assert getattr(texts, bot_key).split("\n")[0] not in html
    assert "بفرست" not in html
