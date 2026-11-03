"""Which orders belong to a service, and whether it is a free trial.

One place for two questions the lifecycle code asks:

* ``load_order_refs``: the orders that reference an account, by its panel
  username or as ``renew_account_id``, in a few chunked queries (no per-account
  loop).
* ``is_trial_by_refs``: **a service is a trial only when every order that
  references it is a trial order** (``trial_kind`` set) and there is at least
  one. A paid order, in any status, that points at the same username or renews
  that account makes it a paid service: a trial is never judged by the first
  order alone, and an imported service with no order at all is not a trial.

The customer's screens ask a narrower question with ``is_trial_account``: it
reads one account's orders and ignores the ones that never reached the service
(unpaid, refused). The two are kept apart on purpose: the deletion job must
err on the side of «paid», a renew button must not be refused over an unpaid
order.

A trial cannot be renewed (owner, 2026-10-03), so it gets no volume warning and
no warning before deletion, and it is deleted on its own, shorter number.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, OrderStatus, PasarGuardAccount

_CHUNK = 300

# An order that never reached the customer's service (unpaid, refused) says
# nothing about what the service is.
_NEVER_DELIVERED = frozenset(
    {OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW, OrderStatus.REJECTED}
)


def _references(account: PasarGuardAccount) -> ColumnElement[bool]:
    return or_(
        Order.panel_username == account.panel_username,
        Order.renew_account_id == account.id,
    )


async def is_trial_account(session: AsyncSession, account: PasarGuardAccount) -> bool:
    """A service is a trial only when at least one order references it and every
    referencing order is a trial order.

    An imported service has no order at all, so it is **not** a trial: the old
    bot's free tests carry no mark here, and refusing a paying customer's renew
    on a guess is worse than offering it on a test. A service the customer then
    paid for (a later order that is not a trial) stops being one.
    """
    rows = (
        await session.scalars(
            select(Order.trial_kind).where(
                _references(account), Order.status.not_in(_NEVER_DELIVERED)
            )
        )
    ).all()
    return bool(rows) and all(kind is not None for kind in rows)


async def account_was_created_by_an_order(
    session: AsyncSession, account: PasarGuardAccount
) -> bool:
    """Did an order of this bot make this panel account (or plainly extend it)?

    An account imported from the old bot has no order at all, so every order
    that ever touches it is a switch or a renew from scratch. Finding one that
    is neither (the creating order, a trial, an ordinary extension) proves the
    bot built the account and owns its panel groups. Without such an order the
    groups are the owner's: a switch or a renew from scratch does not write
    them, however many of those orders the account has by now.
    """
    found = await session.scalar(
        select(Order.id)
        .where(
            _references(account),
            Order.status.not_in(_NEVER_DELIVERED),
            Order.renew_resets.is_(False),
            Order.renew_mode.is_(None),
        )
        .limit(1)
    )
    return found is not None


@dataclass(frozen=True)
class OrderRef:
    id: int
    user_id: int
    status: OrderStatus
    trial_kind: str | None
    panel_username: str | None
    renew_account_id: int | None
    created_at: datetime | None
    paid_at: datetime | None
    activated_at: datetime | None
    updated_at: datetime | None


def aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _chunks(items: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


async def load_order_refs(
    session: AsyncSession, accounts: Sequence[tuple[int, str]]
) -> dict[int, list[OrderRef]]:
    """``{account_id: [orders]}`` for ``(account_id, panel_username)`` pairs."""
    by_name = {name: account_id for account_id, name in accounts}
    ids = [account_id for account_id, _ in accounts]
    names = list(by_name)
    out: dict[int, list[OrderRef]] = {account_id: [] for account_id in ids}
    seen: set[tuple[int, int]] = set()
    clauses: list[ColumnElement[bool]] = []
    clauses.extend(Order.panel_username.in_(list(chunk)) for chunk in _chunks(names))
    clauses.extend(Order.renew_account_id.in_(list(chunk)) for chunk in _chunks(ids))
    for clause in clauses:
        rows = (
            (
                await session.execute(
                    select(
                        Order.id,
                        Order.user_id,
                        Order.status,
                        Order.trial_kind,
                        Order.panel_username,
                        Order.renew_account_id,
                        Order.created_at,
                        Order.paid_at,
                        Order.activated_at,
                        Order.updated_at,
                    ).where(or_(clause))
                )
            )
            .tuples()
            .all()
        )
        for row in rows:
            ref = OrderRef(
                id=int(row[0]),
                user_id=int(row[1]),
                status=row[2],
                trial_kind=row[3],
                panel_username=row[4],
                renew_account_id=int(row[5]) if row[5] is not None else None,
                created_at=aware(row[6]),
                paid_at=aware(row[7]),
                activated_at=aware(row[8]),
                updated_at=aware(row[9]),
            )
            targets = set()
            if ref.panel_username in by_name:
                targets.add(by_name[ref.panel_username])
            if ref.renew_account_id in out:
                targets.add(int(ref.renew_account_id))  # type: ignore[arg-type]
            for account_id in targets:
                if (account_id, ref.id) not in seen:
                    seen.add((account_id, ref.id))
                    out[account_id].append(ref)
    return out


def is_trial_by_refs(refs: Sequence[OrderRef], *, admin_touched: bool = False) -> bool:
    """Only trial orders reference it (and at least one does), and no admin has worked on it.

    An admin who extended a trial or gave it volume (a ``service_operations`` row) made it a
    service the customer may be keeping: paid rules, with the warning, from then on.
    """
    return bool(refs) and not admin_touched and all(ref.trial_kind for ref in refs)


async def admin_touched_ids(session: AsyncSession, account_ids: Sequence[int]) -> set[int]:
    """Accounts with any admin operation (extend days, add volume) on record."""
    from database.models.service_operation import ServiceOperation

    touched: set[int] = set()
    for chunk in _chunks(list(account_ids)):
        touched.update(
            int(i)
            for i in (
                await session.execute(
                    select(ServiceOperation.account_id)
                    .where(ServiceOperation.account_id.in_(list(chunk)))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    return touched


async def trial_account_ids(session: AsyncSession, usernames: Iterable[str]) -> dict[str, int]:
    """``{panel_username: account_id}`` of the usernames that are free-trial services.

    A username with no account row is not listed (it is treated as paid, which keeps the
    old behaviour of every caller).
    """
    from database.models import PasarGuardAccount
    from services.accounts import live_account_clause

    names = sorted({n for n in usernames if n})
    if not names:
        return {}
    pairs: list[tuple[int, str]] = []
    for chunk in _chunks(names):
        pairs += [
            (int(i), str(n))
            for i, n in (
                await session.execute(
                    select(PasarGuardAccount.id, PasarGuardAccount.panel_username).where(
                        PasarGuardAccount.panel_username.in_(list(chunk)),
                        live_account_clause(),
                    )
                )
            ).all()
        ]
    refs = await load_order_refs(session, pairs)
    touched = await admin_touched_ids(session, [i for i, _ in pairs])
    return {
        name: account_id
        for account_id, name in pairs
        if is_trial_by_refs(refs[account_id], admin_touched=account_id in touched)
    }
