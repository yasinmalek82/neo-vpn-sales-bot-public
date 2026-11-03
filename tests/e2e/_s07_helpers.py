"""Helpers for the wallet, security and outage scenarios (E2).

Candidates for the shared harness: ``press_on_last``, ``open_customer_card``,
``users_list``, ``credit_by_service``, ``db_user``, ``ledger``, ``wait_for_trial``,
``button_cb``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from database.models import User, WalletLedgerEntry
from services.customer_lookup import adjust_wallet_manually
from tests.e2e.harness import App
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.telegram import SentMessage

ASCII_DIGIT = re.compile(r"[0-9]")


def button_cb(message: SentMessage, label: str, *, startswith: bool = False) -> str:
    """``callback_data`` of the one button on THIS message whose text fits ``label``."""
    hits = [
        b
        for row in (message.markup.inline_keyboard if message.markup else [])
        for b in row
        if (b.text.startswith(label) if startswith else label in b.text)
    ]
    if len(hits) != 1:
        raise AssertionError(f"{len(hits)} buttons fit {label!r} on {message.rows}")
    assert hits[0].callback_data is not None
    return hits[0].callback_data


async def press_on_last(actor: Actor, label: str, *, startswith: bool = False) -> None:
    """Tap a button on the newest message only (``Actor.tap`` searches every screen)."""
    message = actor.last_message
    await actor.press(button_cb(message, label, startswith=startswith), message=message)


async def users_list(admin: Actor) -> None:
    """/admin → «👥 مشتری‌ها» → «👥 کاربران»."""
    await admin.send("/admin")
    await admin.tap("👥 مشتری‌ها")
    await admin.tap("👥 کاربران")


async def open_customer_card(admin: Actor, name: str) -> None:
    """Open a customer's card from the browse list, by the name on their row."""
    await users_list(admin)
    for _ in range(5):
        message = admin.last_message
        for row in message.rows:
            for text in row:
                if len(row) == 1 and row_name(text) == name:
                    await press_on_last(admin, text)
                    return
        await press_on_last(admin, "بعدی")
    raise AssertionError(f"{name!r} is not on any page of the users list")


def list_names(admin: Actor) -> list[str]:
    """User rows on the screen: every row above the pager and filters."""
    names: list[str] = []
    for row in admin.last_message.rows:
        if len(row) == 1 and not row[0].startswith(("بعدی", "⬅️", "🔎", "🏠", "در حال")):
            names.append(row[0])
    return names


async def db_user(app: App, who: Actor) -> User:
    return await app.one(select(User).where(User.telegram_id == who.user_id))


async def ledger(app: App, who: Actor) -> list[WalletLedgerEntry]:
    user = await db_user(app, who)
    return await app.all(
        select(WalletLedgerEntry)
        .where(WalletLedgerEntry.user_id == user.id)
        .order_by(WalletLedgerEntry.id)
    )


async def credit_by_service(app: App, who: Actor, amount: int, *, stamp: str = "seed") -> None:
    """Put money in a wallet through the real manual-adjust service (a precondition)."""
    user = await db_user(app, who)
    async with app.session() as session:
        await adjust_wallet_manually(
            session,
            user_id=user.id,
            amount_toman=amount,
            admin_telegram_id=app.settings.admin_telegram_ids[0],
            stamp=f"{stamp}:{who.user_id}:{amount}",
        )
        await session.commit()


async def take_trial(who: Actor, group_title: str = "ویژه") -> None:
    """The customer's own path to a free trial of one store group."""
    from bot.keyboards.reply_menu import TRIAL

    await who.tap_reply(TRIAL)
    await press_on_last(who, group_title, startswith=True)


def numbers(text: str) -> list[int]:
    """Every whole number in ``text``, in either digit set, separators removed."""
    from tests.e2e.harness import digits

    return [int(n) for n in re.findall(r"\d+", digits(text))]


def card_value(text: str, label: str) -> int:
    """The number after ``label`` on the card line that starts with it (or contains it)."""
    from tests.e2e.harness import digits

    for line in text.splitlines():
        if label in line:
            after = digits(line.split(label, 1)[1])
            found = re.search(r"\d+", after)
            if found:
                return int(found.group())
    raise AssertionError(f"no {label!r} line with a number in:\n{text}")


def only(items: list[Any]) -> Any:
    assert len(items) == 1, items
    return items[0]


async def admin_changes_wallet(admin: Actor, name: str, amount: int) -> None:
    """The owner's path: card → «🏦 کیف پول» → amount → «✅ تأیید و اعمال»."""
    await open_customer_card(admin, name)
    await admin.tap("🏦 کیف پول")
    await admin.send(str(amount))
    await admin.tap("تأیید و اعمال")


def row_name(label: str) -> str:
    """«🚫رضا • 🏦 ۱۲ هزار» → «رضا»."""
    name = label.split(" • ", maxsplit=1)[0]
    for mark in ("🚫", "⚙️"):
        name = name.removeprefix(mark)
    return name
