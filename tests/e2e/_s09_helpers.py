"""Helpers for sections ۹ to ۱۱ب (package E5): the owner's store screens, as taps.

Candidates for the shared harness: ``plan_titles_on_shelf`` (what a customer sees on a shelf),
``edit_title`` (typing makes the bot edit the admin's own message: see the README trap).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from services.store_admin import CatalogView
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import Actor, App


async def open_store(admin: Actor) -> None:
    """/admin → «🛍 فروشگاه»."""
    await admin.send("/admin")
    await tap_starting(admin, "🛍")


async def skip_promo_rules(admin: Actor) -> None:
    """After «کد درصد» the promo wizard asks four usage questions; «رد کردن» on each keeps the code
    unlimited, which is what a code made before the rules existed was."""
    for _ in range(4):
        await tap_starting(admin, "رد کردن")


async def make_group(
    admin: Actor, title: str, kind: str = "📦", *, parent: str | None = None
) -> None:
    """From the store hub (or a group's dashboard for a child): «➕ گروه جدید», the name, the kind."""
    if parent is not None:
        await tap_starting(admin, "➕ زیرگروه جدید")
    else:
        await tap_starting(admin, "➕ گروه جدید")
    await admin.send(title)
    if kind:
        await tap_starting(admin, kind)


async def set_panel_group(admin: Actor, *names: str) -> None:
    """On a group dashboard: «🖥 پنل» → «🔗 گروه پنل» → tick each panel group → «💾 ذخیره»."""
    await tap_starting(admin, "🖥 پنل")
    await tap_starting(admin, "🔗 گروه پنل")
    for name in names:
        await tap_starting(admin, f"▫️ {name}")
    await tap_starting(admin, "💾 ذخیره")
    await tap_starting(admin, "⬅️ گروه")  # back from the panel section to the dashboard


async def new_plan(
    admin: Actor,
    gb: str = "۵۰ گیگ",
    days: str = "۳۰ روز",
    users: str = "نامحدود",
    price: int = 200_000,
) -> None:
    """«➕ پلن جدید» → volume → duration → devices → price: ends on the confirm page."""
    await tap_starting(admin, "➕ پلن جدید")
    await tap_starting(admin, gb)
    await tap_starting(admin, days)
    await tap_starting(admin, users)
    await admin.send(str(price))


async def live_catalog(app: App) -> CatalogView:
    """The catalog as a customer reads it (published version)."""
    from services.store_admin import get_catalog_view

    async with app.session() as session:
        return await get_catalog_view(session)


async def live_plans(app: App, group_title: str) -> list[str]:
    """Titles of the plans the live catalog holds for one store group."""
    view = await live_catalog(app)
    group = next(c for c in view.categories if c.title_fa == group_title)
    return [p.title_fa for p in view.packages_of(group.id)]


async def drafts(app: App) -> int:
    """How many draft store versions exist. A confirmed edit goes live, so this stays 0."""
    from database.models.store import StoreVersion, StoreVersionStatus

    rows = await app.all(
        select(StoreVersion).where(StoreVersion.status == StoreVersionStatus.draft)
    )
    return len(rows)


async def shelf(customer: Actor, *path: str) -> list[str]:
    """The customer's own way to the shelf: «خرید اشتراک», each group in turn. Returns the buttons."""
    from bot.keyboards.reply_menu import BUY

    if not customer.messages:
        await customer.start()
    await customer.tap_reply(BUY)
    for title in path:
        await tap_starting(customer, title)
    return list(customer.buttons)


async def all_plans(app: App) -> list[str]:
    """Every plan title in the live catalog, whatever its group."""
    return [p.title_fa for p in (await live_catalog(app)).packages]


async def open_cast(admin: Actor) -> None:
    """/admin → «📢 پیام همگانی»."""
    await admin.send("/admin")
    await tap_starting(admin, "📢 پیام همگانی")


async def compose_cast(admin: Actor, body: str) -> None:
    """From the cast home: «✍️ پیام جدید», the text. Ends on the audience question."""
    await tap_starting(admin, "✍️")
    await admin.send(body)


async def send_cast(admin: Actor, body: str, audience: str = "👥 همهٔ") -> None:
    """The whole owner path: compose, audience, «📤 بله، ارسال کن»."""
    await open_cast(admin)
    await compose_cast(admin, body)
    await tap_starting(admin, audience)
    await tap_starting(admin, "📤")


async def people(app: App, count: int) -> list[Actor]:
    """``count`` customers who have pressed /start."""
    out = []
    for i in range(count):
        c = app.customer(f"c{i}")
        await c.start()
        out.append(c)
    return out


async def invite_code(customer: Actor) -> str:
    """Open «🎁 دعوت دوستان» and read the person's own code off the screen."""
    import re

    await customer.tap_reply("🎁 دعوت دوستان")
    screen = next(m.text for m in reversed(customer.messages) if "کد تو:" in m.text)
    found = re.search(r"کد تو: (\w+)", screen)
    assert found, screen
    return found.group(1)


async def open_with_code(customer: Actor, code: str) -> list[str]:
    """``/start <code>`` (the invite link) and what came back."""
    mark = customer.mark()
    await customer.start(payload=code)
    return customer.texts_since(mark)


def styled_buttons(markup: Any) -> dict[str, str | None]:
    """label -> style (``'primary'``, ``'success'``, ``'danger'`` or ``None``) of one keyboard."""
    rows = markup.inline_keyboard if hasattr(markup, "inline_keyboard") else markup.keyboard
    return {str(b.text): (str(b.style) if b.style else None) for row in rows for b in row}


def coloured(markup: Any) -> list[str]:
    """The labels on a keyboard that carry a colour."""
    return [label for label, style in styled_buttons(markup).items() if style]


def rows_of(markup: Any) -> list[list[str]]:
    rows = markup.inline_keyboard if hasattr(markup, "inline_keyboard") else markup.keyboard
    return [[str(b.text) for b in row] for row in rows]


def every_keyboard_sent(app: App) -> list[Any]:
    """Every reply markup the bot ever handed to Telegram (sends and edits)."""
    return [c.params["reply_markup"] for c in app.tg.calls if c.params.get("reply_markup")]


def bottom_keyboard(app: App) -> Any:
    """The newest bottom (reply) keyboard the bot sent."""
    from aiogram.types import ReplyKeyboardMarkup

    found = [m for m in every_keyboard_sent(app) if isinstance(m, ReplyKeyboardMarkup)]
    assert found, "the bot never sent a bottom keyboard"
    return found[-1]
