"""Helpers for section ۱۶ (warnings, and deleting ended services).

Candidates for the shared harness: ``seed_service`` makes one service the way an
import from the old bot leaves it (an account row and a panel user, no order),
and ``seed_order`` the orders of a bought one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from bot.keyboards.lifecycle import CODE_OF_FIELD, LifeCB
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User
from database.models.lifecycle import LifecycleSettings
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import GB, App
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.panel import PanelUser


async def open_lifecycle(admin: Actor) -> None:
    """/admin → ⚙️ تنظیمات → 🗓 هشدار و حذف سرویس."""
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "🗓 هشدار و حذف سرویس")


async def set_number(admin: Actor, field: str, typed: str) -> None:
    """Open the screen, press the field's button, type the value."""
    await open_lifecycle(admin)
    await admin.press(f"{LifeCB.EDIT}{CODE_OF_FIELD[field]}")
    await admin.send(typed)


async def lifecycle_row(app: App) -> LifecycleSettings | None:
    async with app.session() as session:
        return await session.get(LifecycleSettings, 1)


async def configure(app: App, **values: Any) -> None:
    """Write the settings row directly (a scenario about a job, not about the screen)."""
    async with app.session() as session:
        row = await session.get(LifecycleSettings, 1)
        if row is None:
            row = LifecycleSettings(id=1, warn_days=[3, 1], warn_volume_percents=[20, 5])
            session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


async def customer_user(app: App, telegram_id: int, *, reseller: bool = False) -> User:
    async with app.session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                first_name=f"u{telegram_id}",
                referral_code=f"R{telegram_id}"[:16],
                language_code="fa",
                is_reseller=reseller,
            )
            session.add(user)
            await session.commit()
        elif reseller and not user.is_reseller:
            user.is_reseller = True
            await session.commit()
        return user


def panel_user(
    app: App,
    name: str,
    *,
    status: str = "active",
    expires_in_days: float | None = None,
    gb: int | None = None,
    used_gb: float = 0,
) -> PanelUser:
    """A user on the fake panel. ``expires_in_days`` may be negative (already past)."""
    import secrets

    now = datetime.now(UTC)
    user = PanelUser(
        id=len(app.panel.users) + 100,
        username=name,
        token=secrets.token_hex(8),
        created_at=now - timedelta(days=60),
        status=status,
        used_traffic=int(used_gb * GB),
        expire=None if expires_in_days is None else now + timedelta(days=expires_in_days),
        data_limit=None if gb is None else gb * GB,
        group_ids=[1],
    )
    app.panel.users[name] = user
    return user


async def seed_service(
    app: App,
    owner_telegram_id: int,
    name: str,
    *,
    status: str = "active",
    expires_in_days: float | None = None,
    gb: int | None = None,
    used_gb: float = 0,
    reseller: bool = False,
    age_days: int = 60,
    label: str | None = None,
) -> int:
    """An imported-style service: the panel user and the account row, no order.

    The account is aged by default, so «created in the last day» does not shield it.
    """
    user = await customer_user(app, owner_telegram_id, reseller=reseller)
    puser = panel_user(
        app, name, status=status, expires_in_days=expires_in_days, gb=gb, used_gb=used_gb
    )
    aged = datetime.now(UTC) - timedelta(days=age_days)
    async with app.session() as session:
        account = PasarGuardAccount(
            user_id=user.id,
            panel_user_id=puser.id,
            panel_username=name,
            subscription_url=f"https://panel.test/sub/{puser.token}",
            display_name=label or f"svc {name}",
            service_name=name,
            created_at=aged,
            updated_at=aged,
        )
        session.add(account)
        await session.commit()
        return int(account.id)


async def seed_order(
    app: App,
    owner_telegram_id: int,
    username: str,
    *,
    status: OrderStatus,
    trial_kind: str | None = None,
    age_days: int = 60,
    renew_account_id: int | None = None,
    price_toman: int = 150_000,
    expires_in_hours: float | None = None,
) -> int:
    """One order row for an account, aged so it is not «recent»."""
    async with app.session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == owner_telegram_id))
        ).scalar_one()
        plan = (await session.execute(select(Plan).limit(1))).scalar_one_or_none()
        if plan is None:
            from database.models.plan import DataLimitResetStrategy

            plan = Plan(
                name="lifecycle-test",
                description="t",
                data_limit_gb=10,
                duration_days=30,
                price_toman=150_000,
                data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
                group_ids=[1],
                hwid_limit=0,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
        stamp = datetime.now(UTC) - timedelta(days=age_days)
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            status=status,
            price_toman=price_toman,
            discount_toman=0,
            panel_username=username,
            trial_kind=trial_kind,
            renew_account_id=renew_account_id,
            created_at=stamp,
            updated_at=stamp,
            paid_at=stamp,
            activated_at=stamp if status == OrderStatus.ACTIVE else None,
            expires_at=(
                stamp + timedelta(days=30)
                if expires_in_hours is None
                else datetime.now(UTC) + timedelta(hours=expires_in_hours)
            ),
        )
        session.add(order)
        await session.commit()
        return int(order.id)


# --- section ۱۶, deleting ended services ------------------------------------------------


async def run_lifecycle(app: App) -> Any:
    """The daily job, with the arguments production registers."""
    return await app.run_job("lifecycle")


def panel_deletes(app: App) -> list[str]:
    """The usernames the bot asked the panel to delete, in order."""
    return [
        r.path.rsplit("/", 1)[-1]
        for r in app.panel.requests
        if r.method == "DELETE" and r.status in (200, 204, 404)
    ]


async def account_state(app: App, name: str) -> PasarGuardAccount:
    async with app.session() as session:
        return (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.panel_username == name)
            )
        ).scalar_one()


async def is_deleted(app: App, name: str) -> bool:
    return (await account_state(app, name)).deleted_at is not None


async def money_rows(app: App) -> tuple[int, int, int]:
    """(orders, payments, wallet ledger entries): deleting a service changes none of them."""
    from sqlalchemy import func

    from database.models import Payment
    from database.models.wallet import WalletLedgerEntry

    async with app.session() as session:
        return (
            int(await session.scalar(select(func.count()).select_from(Order)) or 0),
            int(await session.scalar(select(func.count()).select_from(Payment)) or 0),
            int(await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) or 0),
        )


async def age_delete_warnings(app: App, *, days: int) -> int:
    """Make every sent deletion warning ``days`` older. Returns how many there were."""
    from database.models.lifecycle import NOTICE_DELETE_WARNING, ServiceNotice

    async with app.session() as session:
        ids = (
            (
                await session.execute(
                    select(ServiceNotice.id).where(ServiceNotice.kind == NOTICE_DELETE_WARNING)
                )
            )
            .scalars()
            .all()
        )
    for notice_id in ids:
        await app.age(
            ServiceNotice,
            int(notice_id),
            days=days,
            columns=("created_at", "sent_at", "delivered_at"),
        )
    return len(ids)


async def backdate_limited_since(app: App, name: str, *, days: int) -> None:
    async with app.session() as session:
        account = (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.panel_username == name)
            )
        ).scalar_one()
        account.limited_since = datetime.now(UTC) - timedelta(days=days)
        await session.commit()


def before_nth_read(app: App, username: str, nth: int, action: Any) -> None:
    """Run ``action()`` just before the panel answers the ``nth`` GET of ``username``.

    ``nth`` counts from 1: ``before_nth_read(app, "svc", 2, renew)`` changes the panel
    between the job's first read and its re-read. Chains with the harness's own hook.
    """
    previous = app.panel.on_network
    fired = {"done": False}

    def hook() -> None:
        if previous is not None:
            previous()
        gets = [
            r
            for r in app.panel.requests
            if r.method == "GET" and r.path.rsplit("/", 1)[-1] == username
        ]
        if len(gets) == nth - 1 and not fired["done"]:
            fired["done"] = True
            action()

    app.panel.on_network = hook


def sqlite_exec(app: App, sql: str, *params: Any) -> None:
    """A write from outside the bot, as another process would do it (for hooks)."""
    import sqlite3

    conn = sqlite3.connect(app.db_path, timeout=5)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
