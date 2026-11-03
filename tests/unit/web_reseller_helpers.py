"""Shared pieces for the Mini App's reseller tests: a seeded catalog, a shop, snapshots.

The catalog has two groups. «حجمی» has shelf tabs (30 and 60 days, economy and premium) and
two plans; «ساده» is a flat list (each plan priced at its own duration). The cells:

    حجمی  ۵۰ گیگ:  30d economy 150k, 60d economy 270k, 30d premium 200k
    حجمی  ۱۰۰ گیگ: 30d economy 250k
    ساده  ۱۰ گیگ:  30d 60k          ساده  ۵۰ گیگ: 60d 255k
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from bot.handlers import admin as bot_admin
from bot.keyboards.admin import AdminCB
from database.models import ResellerPlanPrice, ResellerPriceOp, ResellerRate, User
from database.models.store import StorePackage, StorePackageKind, StorePackagePrice
from services.store_admin import (
    add_category_duration,
    commit_and_activate,
    create_draft_category,
    create_draft_package,
    get_catalog_view,
    get_or_create_draft,
    set_category_flat_plan_list,
)
from services.store_pricing import DEFAULT_TIER, PREMIUM_TIER
from tests.unit.web_panel_helpers import ENV_ADMIN as ADMIN_TG

Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Catalog:
    tabbed: str  # category slug of «حجمی»
    flat: str  # category slug of «ساده»
    a50: str  # plan slugs
    a100: str
    f10: str
    f50: str


async def seed_catalog(factory: Factory) -> Catalog:
    async with factory() as s:
        tabbed = await create_draft_category(s, title_fa="حجمی", kind="volume")
        await add_category_duration(s, tabbed.id, months=2)
        tabbed.sales_flags = {**dict(tabbed.sales_flags or {}), "tiers_enabled": True}
        flag_modified(tabbed, "sales_flags")
        a50 = await create_draft_package(
            s,
            category_id=tabbed.id,
            title_fa="۵۰ گیگ",
            fixed_price_toman=150_000,
            volume_gb=50,
            duration_days=30,
        )
        await create_draft_package(
            s,
            category_id=tabbed.id,
            title_fa="۱۰۰ گیگ",
            fixed_price_toman=250_000,
            volume_gb=100,
            duration_days=30,
        )
        draft = await get_or_create_draft(s)
        for days, tier, price in ((60, DEFAULT_TIER, 270_000), (30, PREMIUM_TIER, 200_000)):
            s.add(
                StorePackagePrice(
                    store_version_id=draft.id,
                    package_slug=str(a50.slug),
                    duration_days=days,
                    tier=tier,
                    price_toman=price,
                )
            )
        flat = await create_draft_category(s, title_fa="ساده", kind="volume")
        await set_category_flat_plan_list(s, flat.id, enabled=True)
        await create_draft_package(
            s,
            category_id=flat.id,
            title_fa="۱۰ گیگ ساده",
            fixed_price_toman=60_000,
            volume_gb=10,
            duration_days=30,
        )
        f50 = await create_draft_package(
            s,
            category_id=flat.id,
            title_fa="۵۰ گیگ ساده",
            fixed_price_toman=255_000,
            volume_gb=50,
            duration_days=60,
        )
        await commit_and_activate(s, notes="seed")
        await s.commit()
        view = await get_catalog_view(s)
        slug = {c.title_fa: str(c.slug) for c in view.categories}
        plan = {p.title_fa: str(p.slug) for p in view.packages}
        assert str(a50.slug) in plan.values() and str(f50.slug) in plan.values()
        return Catalog(
            tabbed=slug["حجمی"],
            flat=slug["ساده"],
            a50=plan["۵۰ گیگ"],
            a100=plan["۱۰۰ گیگ"],
            f10=plan["۱۰ گیگ ساده"],
            f50=plan["۵۰ گیگ ساده"],
        )


async def add_shop(
    factory: Factory, telegram_id: int = 7001, *, name: str = "موبایل", reseller: bool = True
) -> int:
    async with factory() as s:
        user = User(
            telegram_id=telegram_id,
            username=f"shop{telegram_id}",
            first_name=name,
            referral_code=f"SHOP{telegram_id}",
            is_reseller=reseller,
        )
        s.add(user)
        await s.commit()
        return int(user.id)


async def snapshot(factory: Factory, user_id: int) -> dict[str, Any]:
    """Every reseller row of one shop as plain facts, so two databases can be compared."""
    async with factory() as s:
        prices = (
            await s.execute(select(ResellerPlanPrice).where(ResellerPlanPrice.user_id == user_id))
        ).scalars()
        rate = (
            await s.execute(select(ResellerRate).where(ResellerRate.user_id == user_id))
        ).scalar_one_or_none()
        ops = (
            await s.execute(select(ResellerPriceOp).where(ResellerPriceOp.user_id == user_id))
        ).scalars()
        return {
            "prices": sorted(
                (
                    r.package_slug,
                    -1 if r.duration_days is None else r.duration_days,
                    r.tier,
                    r.price_toman,
                )
                for r in prices
            ),
            "rate": (
                None
                if rate is None
                else (
                    rate.price_per_gb_toman,
                    rate.price_per_day_toman,
                    rate.trial_quota_monthly,
                    rate.trial_price_toman,
                )
            ),
            "ops": sorted(
                (
                    -1 if o.duration_days is None else o.duration_days,
                    o.tier,
                    sorted((o.before_json or {}).items()),
                )
                for o in ops
            ),
        }


def hidden(html: str, name: str) -> list[str]:
    """Every value of the hidden input called ``name`` on the page."""
    return re.findall(rf'<input type="hidden" name="{name}" value="([^"]*)"', html)


def form_for(html: str, action: str) -> str:
    """The first ``<form>`` that posts to ``action`` (a path suffix), as text."""
    for m in re.finditer(r"<form[^>]*>.*?</form>", html, flags=re.S):
        if f'hx-post="{action}"' in m.group(0) or re.search(
            rf'hx-post="[^"]*{re.escape(action)}"', m.group(0)
        ):
            return m.group(0)
    raise AssertionError(f"no form posts to {action}")


async def add_custom_template(factory: Factory, category_slug: str) -> str:
    """A custom-purchase template in a group (it has a retail cell, but no price of its own
    to override). Returns its slug."""
    async with factory() as s:
        draft = await get_or_create_draft(s)
        cat = next(c for c in draft.categories if str(c.slug) == category_slug)
        slug = f"{category_slug}-custom"
        s.add(
            StorePackage(
                store_version_id=draft.id,
                category_id=cat.id,
                slug=slug,
                kind=StorePackageKind.custom_template,
                title_fa="ساخت دلخواه",
                enabled=True,
                sort_order=999,
                fixed_price_toman=None,
                options={"volume_gb": 10, "duration_days": 30},
                sales_flags={},
            )
        )
        s.add(
            StorePackagePrice(
                store_version_id=draft.id,
                package_slug=slug,
                duration_days=30,
                tier=DEFAULT_TIER,
                price_toman=99_000,
            )
        )
        await commit_and_activate(s, notes="custom")
        await s.commit()
        return slug


async def republish(factory: Factory) -> None:
    """A catalog edit that mints new row ids for every group and plan (slugs stay)."""
    async with factory() as s:
        await get_or_create_draft(s)
        await commit_and_activate(s, notes="republish")
        await s.commit()


async def package_row_ids(factory: Factory) -> dict[str, int]:
    async with factory() as s:
        view = await get_catalog_view(s)
        return {str(p.slug): int(p.id) for p in view.packages}


# --------------------------------------------------------------------------- #
# The bot's real handlers, driven on a database of their own
# --------------------------------------------------------------------------- #


def bot_settings() -> SimpleNamespace:
    return SimpleNamespace(admin_telegram_ids=[ADMIN_TG], store_owner_telegram_ids=[])


def bot_message(text: str | None = None) -> MagicMock:
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.from_user = SimpleNamespace(id=ADMIN_TG)
    message.text = text
    return message


def bot_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=ADMIN_TG)
    callback.answer = AsyncMock()
    callback.message = bot_message()
    return callback


def bot_fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_TG, user_id=ADMIN_TG),
    )


def fake_bot_screens(monkeypatch: Any) -> None:
    """`_tg_message` does an isinstance check a fake cannot satisfy."""
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(bot_admin, "_tg_message", lambda _cb: screen)


async def bot_open(
    s: AsyncSession,
    state: FSMContext,
    uid: int,
    category: str,
    d: int | None,
    tier: str,
) -> None:
    """The matrix screen the owner has open: group, duration tab, tier tab."""
    view = await get_catalog_view(s)
    cid = next(c.id for c in view.categories if str(c.slug) == category)
    await state.update_data(
        rs_user_id=uid, rs_category_id=cid, rs_sel_duration_days=d, rs_sel_tier=tier
    )
    await bot_admin._show_reseller_matrix(bot_message(), s, state, uid)


async def bot_type(s: AsyncSession, state: FSMContext, text: str) -> MagicMock:
    reply = bot_message(text)
    await bot_admin.admin_reseller_number_save(reply, state, s, bot_settings(), None)
    return reply


def said(mock: MagicMock) -> str:
    """The first text the bot sent through ``answer`` (a refusal line)."""
    return str(mock.answer.await_args.args[0])


async def bot_set_price(
    s: AsyncSession,
    uid: int,
    category: str,
    slug: str,
    d: int | None,
    tier: str,
    text: str,
) -> MagicMock:
    state = bot_fsm()
    await bot_open(s, state, uid, category, d, tier)
    await bot_admin.admin_reseller_matrix_cell(
        bot_callback(f"{AdminCB.RS_CELL}{slug}"), state, s, bot_settings(), None
    )
    return await bot_type(s, state, text)


async def bot_clear(
    s: AsyncSession, uid: int, category: str, slug: str, d: int | None, tier: str
) -> None:
    state = bot_fsm()
    await bot_open(s, state, uid, category, d, tier)
    await bot_admin.admin_reseller_plan_clear(
        bot_callback(f"{AdminCB.RS_PLAN_CLEAR}{uid}:{slug}"), state, s, bot_settings(), None
    )


async def bot_rate(s: AsyncSession, uid: int, field: str, text: str) -> MagicMock:
    state = bot_fsm()
    await bot_admin.admin_reseller_rate_ask(
        bot_callback(f"{AdminCB.RS_RATE}{uid}:{field}"), state, bot_settings(), None
    )
    return await bot_type(s, state, text)


async def bot_cut(
    s: AsyncSession,
    uid: int,
    category: str,
    d: int | None,
    tier: str,
    text: str,
    *,
    state: FSMContext | None = None,
) -> MagicMock:
    """Tap «کاهش درصدی», type ``text``, and confirm if the bot showed a preview."""
    state = state or bot_fsm()
    await bot_open(s, state, uid, category, d, tier)
    await bot_admin.admin_reseller_pct_ask(
        bot_callback(AdminCB.RS_PCT), state, bot_settings(), None
    )
    reply = await bot_type(s, state, text)
    if (await state.get_data()).get("rs_pct_changes"):
        await bot_admin.admin_reseller_pct_confirm(
            bot_callback(AdminCB.RS_PCT_OK), state, s, bot_settings(), None
        )
    return reply


async def bot_undo(s: AsyncSession, uid: int, category: str, d: int | None, tier: str) -> MagicMock:
    state = bot_fsm()
    await bot_open(s, state, uid, category, d, tier)
    callback = bot_callback(AdminCB.RS_UNDO)
    await bot_admin.admin_reseller_undo(callback, state, s, bot_settings(), None)
    return callback


async def dump_db(factory: Factory) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, to prove a request changed nothing at all."""
    from sqlalchemy import text

    from database.base import Base

    out: dict[str, list[tuple[Any, ...]]] = {}
    async with factory() as s:
        for table in Base.metadata.sorted_tables:
            rows = (await s.execute(text(f'SELECT * FROM "{table.name}"'))).all()  # noqa: S608
            out[table.name] = sorted(tuple(map(str, row)) for row in rows)
    return out
