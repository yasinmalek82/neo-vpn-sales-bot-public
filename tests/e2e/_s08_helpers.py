"""Helpers for section ۸ (renewal and the owner's service desk), ۱۴ and ۱۵.

Candidates for the shared harness: ``enable_downloads`` (the fake Telegram refuses
every file download, so a photo the bot fetches with ``bot.download`` cannot be
tested without it), ``buy_active`` (one paid, delivered volume service).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

from PIL import Image

from bot.keyboards.reply_menu import ACCOUNT
from tests.e2e._s07b_helpers import approve_order, open_card, tap_starting
from tests.e2e.harness import App, Store
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.panel import PanelUser


def enable_downloads(app: App) -> None:
    """Let ``bot.download(file_id)`` return the bytes a person sent.

    ``getFile`` answers with the file id as its path, and the download stream
    serves ``app.tg.files[file_id]``.
    """
    from aiogram.types import File

    tg = app.tg

    async def get_file(bot: Any, method: Any) -> Any:
        return File(
            file_id=method.file_id, file_unique_id=method.file_id[:12], file_path=method.file_id
        )

    async def stream(url: str, *_: Any, **__: Any) -> Any:
        yield tg.files[url.rsplit("/", 1)[-1]]

    tg._m_GetFile = get_file  # type: ignore[method-assign]
    tg.stream_content = stream  # type: ignore[method-assign,assignment]


def square_png(color: tuple[int, int, int], side: int = 800) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (side, side), color).save(out, format="PNG")
    return out.getvalue()


async def buy_active(
    app: App,
    store: Store,
    customer: Actor,
    plan_key: str = "special_50",
    name: str = "ali12",
) -> PanelUser:
    """The customer's own paid path to a delivered service. Returns its panel account.

    A second service of a shape the customer already owns meets «تمدید یا جدید»:
    this always answers «✨ سرویس جدید».
    """
    from bot.texts import texts
    from tests.e2e.harness import browse_to_plan, plain

    if customer._registered is False or not customer.messages:  # noqa: SLF001
        await customer.start()
    await browse_to_plan(customer, store, plan_key)
    await customer.send(name)
    if customer.last_text == plain(texts.purchase_renew_or_new):
        await customer.tap("✨ سرویس جدید")
    await customer.tap("بدون کد تخفیف")
    await customer.tap("ثبت سفارش")
    order = await app.latest_order(customer)
    await customer.send_photo()
    await approve_order(app, order.id)
    return max(app.panel.users.values(), key=lambda u: u.id)


async def open_service(admin: Actor, customer_name: str, service_prefix: str | None = None) -> None:
    """Card → «📱 سرویس‌ها» → the service row (the first one unless a prefix is given)."""
    await open_card(admin, customer_name)
    await tap_starting(admin, "📱 سرویس")
    label = next(
        b
        for b in admin.buttons
        if b[:1] in "🟢🔴⛔" and (service_prefix is None or service_prefix in b)
    )
    await admin.tap(label)


async def my_service(customer: Actor, name_prefix: str | None = None) -> None:
    """«سرویس‌های من» → one service."""
    await customer.tap_reply(ACCOUNT)
    label = next(
        b
        for b in customer.buttons
        if b[:1] in "🟢🟡🔴⛔" and (name_prefix is None or name_prefix in b)
    )
    await customer.tap(label)


def expire_in(user: PanelUser, *, days: float, hours: float = 12) -> None:
    """Put the account's expiry ``days`` (and a half day, away from rounding edges) ahead."""
    user.expire = datetime.now(UTC) + timedelta(days=days, hours=hours)


async def retire_plan(app: App, store: Store, plan_key: str) -> None:
    """Take a plan off sale for good, as the owner does with an old product."""
    from services.store_admin import commit_and_activate, delete_draft_package, list_draft_packages

    plan = store.plans[plan_key]
    async with app.session() as session:
        packages = await list_draft_packages(session, category_id=store.category_ids[plan.group])
        target = next(p for p in packages if p.title_fa == plan.title)
        await delete_draft_package(session, int(target.id))
        await commit_and_activate(session, notes="پلن قدیمی برداشته شد")
