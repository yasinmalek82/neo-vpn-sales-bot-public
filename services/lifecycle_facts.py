"""Load what ``decide`` needs about services, in a handful of queries.

Plain data out (``AccountFacts``), no ORM row crosses a network await. Only live
(not soft-deleted) accounts are loaded, through ``live_account_clause``.

* **Open orders.** Any order of the SAME USER that is pending payment, in review,
  approved or provisioning keeps every service of that user for the run: a
  renewal can match by plan shape at provisioning, and a customer who paid for a
  renewal the admin has not approved yet must not lose the service. A
  PROVISION_FAILED order keeps only the account it references.
* **Last change.** The newest of: account created, any referencing order created,
  paid, activated or updated, any service operation (extend, add volume).
* **Warned.** The sent deletion warnings of the account, by event key.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import OrderStatus, PasarGuardAccount, User
from database.models.lifecycle import NOTICE_DELETE_WARNING, ServiceNotice
from database.models.order import Order
from database.models.service_operation import ServiceOperation
from services.account_orders import (
    OrderRef,
    admin_touched_ids,
    aware,
    is_trial_by_refs,
    load_order_refs,
)
from services.accounts import live_account_clause, service_list_label
from services.service_cleanup import AccountFacts

OPEN_ORDER_STATUSES = (
    OrderStatus.PENDING_PAYMENT,
    OrderStatus.PAYMENT_REVIEW,
    OrderStatus.PAYMENT_APPROVED,
    OrderStatus.PROVISIONING,
)
_CHUNK = 300


def _chunks(items: Sequence[int]) -> list[Sequence[int]]:
    return [items[i : i + _CHUNK] for i in range(0, len(items), _CHUNK)]


def _owner_label(user: User) -> str:
    name = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


def service_title(account: PasarGuardAccount) -> str:
    """The name the admin's service lists give a service."""
    return str(account.display_name or account.service_name or account.panel_username)


async def load_facts(
    session: AsyncSession, *, account_ids: Sequence[int] | None = None
) -> list[AccountFacts]:
    """Facts for every live account, or just ``account_ids`` (the ones still live)."""
    statement = (
        select(PasarGuardAccount, User)
        .join(User, User.id == PasarGuardAccount.user_id)
        .where(live_account_clause())
        .order_by(PasarGuardAccount.id)
    )
    if account_ids is not None:
        statement = statement.where(PasarGuardAccount.id.in_(list(account_ids)))
    rows = (await session.execute(statement)).all()
    if not rows:
        return []
    accounts = [(int(a.id), str(a.panel_username)) for a, _ in rows]
    ids = [account_id for account_id, _ in accounts]
    user_ids = sorted({int(u.id) for _, u in rows})

    refs = await load_order_refs(session, accounts)
    touched = await admin_touched_ids(session, ids)

    open_users: set[int] = set()
    for chunk in _chunks(user_ids):
        open_users.update(
            int(uid)
            for uid in (
                await session.execute(
                    select(Order.user_id)
                    .where(Order.user_id.in_(list(chunk)), Order.status.in_(OPEN_ORDER_STATUSES))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

    last_op: dict[int, datetime] = {}
    warned: dict[int, dict[str, datetime]] = {}
    undelivered: dict[int, set[str]] = {}
    for chunk in _chunks(ids):
        for account_id, created, applied in (
            await session.execute(
                select(
                    ServiceOperation.account_id,
                    ServiceOperation.created_at,
                    ServiceOperation.applied_at,
                ).where(ServiceOperation.account_id.in_(list(chunk)))
            )
        ).all():
            for moment in (aware(created), aware(applied)):
                if moment and (int(account_id) not in last_op or moment > last_op[int(account_id)]):
                    last_op[int(account_id)] = moment
        for account_id, key, sent, failed in (
            await session.execute(
                select(
                    ServiceNotice.account_id,
                    ServiceNotice.event_key,
                    ServiceNotice.delivered_at,
                    ServiceNotice.undelivered,
                ).where(
                    ServiceNotice.account_id.in_(list(chunk)),
                    ServiceNotice.kind == NOTICE_DELETE_WARNING,
                    ServiceNotice.delivered_at.is_not(None),
                )
            )
        ).all():
            sent_at = aware(sent)
            if sent_at is not None:
                warned.setdefault(int(account_id), {})[str(key)] = sent_at
                if failed:
                    undelivered.setdefault(int(account_id), set()).add(str(key))

    facts: list[AccountFacts] = []
    for account, user in rows:
        account_id = int(account.id)
        mine: list[OrderRef] = refs.get(account_id, [])
        moments = [aware(account.created_at), last_op.get(account_id)]
        for ref in mine:
            moments += [ref.created_at, ref.paid_at, ref.activated_at, ref.updated_at]
        facts.append(
            AccountFacts(
                account_id=account_id,
                panel_user_id=(
                    int(account.panel_user_id) if account.panel_user_id is not None else None
                ),
                panel_username=str(account.panel_username),
                created_at=aware(account.created_at),
                limited_since=aware(account.limited_since),
                delete_requested_at=aware(account.delete_requested_at),
                delete_exempt=bool(account.delete_exempt),
                is_trial=is_trial_by_refs(mine, admin_touched=account_id in touched),
                has_open_order=int(user.id) in open_users
                or any(
                    ref.status in OPEN_ORDER_STATUSES or ref.status == OrderStatus.PROVISION_FAILED
                    for ref in mine
                ),
                last_change_at=max((m for m in moments if m is not None), default=None),
                warned=warned.get(account_id, {}),
                undelivered=frozenset(undelivered.get(account_id, set())),
                user_id=int(user.id),
                owner_telegram_id=int(user.telegram_id),
                owner_banned=bool(user.is_banned),
                owner_is_reseller=bool(user.is_reseller),
                owner_label=_owner_label(user),
                label=service_list_label(account, reseller=bool(user.is_reseller)),
                title=service_title(account),
            )
        )
    return facts
