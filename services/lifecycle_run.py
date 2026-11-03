"""The daily job that deletes ended services, and the approval of a stored list.

Order of one run (``run_lifecycle``), every step on the side of not deleting:

1. ``delete_mode`` off: read nothing, write nothing (only queued customer notices
   from an earlier run are sent).
2. Load the facts, read the panel ONE user at a time (politely), and decide on what
   the panel says now. A user the panel could not answer for is KEEP. A 404 is KEEP
   (a wrong panel address answers 404 for everyone) except for a service that already
   has a committed delete intent AND while the panel still knows other users.
3. Bookkeeping in one short transaction: ``limited_since`` set the first time a run
   sees a service limited and cleared the first time it sees anything else; an intent
   whose user is alive and no longer qualifies is cleared; an intent whose user is gone
   is finished (the soft delete).
4. ``rehearsal``: delete nothing, set ``last_rehearsal_at``, queue ONE report.
5. ``on``: the HARD STOP first (see ``hard_stop_reached``): delete nothing, store the
   list as a batch for the owner, alert once. Otherwise each DELETE is re-read from
   the panel and re-decided right before its delete (``delete_one``), in two phases:
   the intent is COMMITTED, then the panel is asked, then the row is soft-deleted.
   A crash between the last two converges on the next run.
6. Warnings go to the customer only in ``on`` (claim, commit, send), and customer
   notices of deleted services are sent after the commit. No Telegram send and no
   panel call ever happens inside a transaction.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import PasarGuardClient
from api_client.exceptions import PasarGuardNotFoundError
from api_client.factory import get_pasarguard_client
from bot.config import Settings
from bot.keyboards.account import service_detail_keyboard
from bot.keyboards.lifecycle import buy_keyboard
from bot.texts import texts
from database.base import end_read_transaction
from database.models import PasarGuardAccount, User
from database.models.lifecycle import (
    BATCH_KIND_MANUAL,
    BATCH_KIND_PREVIEW,
    BATCH_KIND_STOP,
    DELETE_MODE_ON,
    DELETE_MODE_REHEARSAL,
    ITEM_DELETE,
    ITEM_WARN,
    NOTICE_DELETE_WARNING,
    NOTICE_DELETED,
    ServiceNotice,
)
from database.models.report import TOPIC_ERRORS, TOPIC_SYSTEM
from services.accounts import live_account_clause, service_list_label
from services.lifecycle_batches import (
    ItemData,
    batch_items,
    claim_batch,
    create_batch,
    get_batch,
    reason_fa,
    same_open_list,
    void_reason,
)
from services.lifecycle_facts import load_facts
from services.lifecycle_settings import LifecycleConfig, load_config, record_rehearsal
from services.report_outbox import enqueue_report
from services.service_cleanup import (
    DELETE,
    WARN,
    AccountFacts,
    Decision,
    decide,
    delete_warning_key,
    hard_stop_reached,
    status_of,
)
from services.service_notices import (
    STALE_CLAIM,
    claim_queued,
    is_permanent_send_failure,
    mark_delivered,
    queue_notice,
    release_notice,
    send_notice_once,
)
from services.ux_format import format_jalali, to_persian_digits
from services.volume_warnings import period_of

logger = logging.getLogger(__name__)

DEFAULT_PAUSE_SECONDS = 0.05
READ_OK = "ok"
READ_MISSING = "missing"
READ_ERROR = "error"
REPORT_LIST_CAP = 15
NOTE_WARNING_UNDELIVERED = "warning_undelivered"

# One run (or one approval) at a time: two overlapping would each decide on the same rows.
_run_lock = asyncio.Lock()


class BatchRefused(Exception):
    """An approval that must not run. ``code`` is one of the ``admin_life_refused_*`` keys."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Outcome:
    account_id: int
    title: str
    status: str  # deleted | skipped | warned
    reason: str
    # «warning_undelivered»: deleted although its warning never reached the customer
    # (he blocked the bot). The owner sees it in the result and the report.
    note: str = ""


@dataclass
class Evaluated:
    facts: AccountFacts
    read: str
    panel_user: Any | None
    decision: Decision


@dataclass
class LifecycleRunResult:
    mode: str = "off"
    services: int = 0
    deleted: int = 0
    warned: int = 0
    skipped: int = 0
    hard_stopped: bool = False
    batch_id: int | None = None
    busy: bool = False
    outcomes: list[Outcome] = field(default_factory=list)


# --- reading the panel -----------------------------------------------------------------------


async def read_panel(client: PasarGuardClient, username: str) -> tuple[str, Any | None]:
    try:
        return READ_OK, await client.get_user(username)
    except PasarGuardNotFoundError:
        return READ_MISSING, None
    except Exception as exc:
        logger.warning("Lifecycle: panel read failed for %s (%s)", username, type(exc).__name__)
        return READ_ERROR, None


async def evaluate(
    client: PasarGuardClient,
    facts_list: Sequence[AccountFacts],
    config: LifecycleConfig,
    now: datetime,
    *,
    pause: float | None,
) -> list[Evaluated]:
    """Read every service's panel user, one at a time, and decide. No database here."""
    out: list[Evaluated] = []
    for index, facts in enumerate(facts_list):
        delay = DEFAULT_PAUSE_SECONDS if pause is None else pause
        if index and delay > 0:
            await asyncio.sleep(delay)
        read, user = await read_panel(client, facts.panel_username)
        out.append(Evaluated(facts, read, user, decide(facts, user, config, now)))
    return out


# --- writes that are one short transaction -------------------------------------------------------


async def _soft_delete(session: AsyncSession, account_id: int, now: datetime) -> bool:
    """Mark the account deleted and queue the customer's notice. The caller commits."""
    result = await session.execute(
        update(PasarGuardAccount)
        .where(PasarGuardAccount.id == account_id, PasarGuardAccount.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    if int(result.rowcount or 0) != 1:  # type: ignore[attr-defined]
        return False
    account = await session.get(PasarGuardAccount, account_id)
    owner = await session.get(User, int(account.user_id)) if account is not None else None
    if owner is not None and not owner.is_banned:
        await queue_notice(
            session,
            event_key=f"deleted:{account_id}:{int(now.timestamp())}",
            kind=NOTICE_DELETED,
            account_id=account_id,
            user_id=int(owner.id),
        )
    return True


# An intent older than this is VOID: cleared, never finished. A 404 hours or days after the
# bot meant to delete says nothing about this moment.
INTENT_VALID = timedelta(hours=48)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _apply_bookkeeping(
    session_factory: async_sessionmaker[Any],
    evaluated: Sequence[Evaluated],
    *,
    now: datetime,
    finish: bool,
) -> int:
    """limited_since, intents. Returns how many soft deletes were finished.

    Intents are cleared in every mode (their user is alive and no longer qualifies, or the
    intent is void); they are FINISHED (the soft delete) only in ``on``, only while valid, and
    only while the panel still answers for other users.
    """
    ok_reads = sum(1 for e in evaluated if e.read == READ_OK)
    finished = 0
    async with session_factory() as session:
        for e in evaluated:
            f = e.facts
            if e.read == READ_OK:
                limited = status_of(e.panel_user)
                restart = (
                    limited == "limited"
                    and f.limited_since is not None
                    and e.decision.reason == "limited_first_seen"
                )
                if limited == "limited" and (f.limited_since is None or restart):
                    # First sighting, or the service was changed after the old clock began.
                    await session.execute(
                        update(PasarGuardAccount)
                        .where(PasarGuardAccount.id == f.account_id)
                        .values(limited_since=now)
                    )
                elif limited != "limited" and f.limited_since is not None:
                    await session.execute(
                        update(PasarGuardAccount)
                        .where(
                            PasarGuardAccount.id == f.account_id,
                            PasarGuardAccount.limited_since.is_not(None),
                        )
                        .values(limited_since=None)
                    )
            if f.delete_requested_at is None:
                continue
            void = now - _aware(f.delete_requested_at) > INTENT_VALID
            if (
                e.read == READ_OK
                and e.decision.verdict != DELETE
                or e.read == READ_MISSING
                and void
            ):
                clear = True
            else:
                clear = False
            if clear:
                await session.execute(
                    update(PasarGuardAccount)
                    .where(PasarGuardAccount.id == f.account_id)
                    .values(delete_requested_at=None)
                )
            elif (
                e.read == READ_MISSING
                # The ONLY 404 that leads to a soft delete: a committed, still valid intent,
                # and the panel still answers for other users (so it is the right panel).
                and finish
                and ok_reads > 0
                and await _soft_delete(session, f.account_id, now)
            ):
                finished += 1
        await session.commit()
    return finished


# --- customer messages -----------------------------------------------------------------------------


def _fa(n: int) -> str:
    return to_persian_digits(n)


async def send_delete_warning(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    facts: AccountFacts,
    panel_user: Any,
    decision: Decision,
    config: LifecycleConfig,
    now: datetime,
) -> bool:
    """The «will be deleted in n days» message, once per period (claimed before the send)."""
    period = period_of(
        int(getattr(panel_user, "data_limit", 0) or 0), getattr(panel_user, "expire", None)
    )
    date = format_jalali(decision.delete_on, persian_digits=True).split(" ")[0]
    text = texts.notify_delete_warning.format(
        name=html.escape(facts.label),
        days=_fa(config.delete_warn_days_before),
        date=date,
    )
    return await send_notice_once(
        session_factory,
        bot,
        event_key=delete_warning_key(facts.account_id, period),
        kind=NOTICE_DELETE_WARNING,
        account_id=facts.account_id,
        user_id=facts.user_id,
        chat_id=facts.owner_telegram_id,
        text=text,
        markup=service_detail_keyboard(facts.account_id, has_link=False, show_renew=True),
        now=now,
    )


async def send_deleted_notices(session_factory: async_sessionmaker[Any], bot: Bot) -> int:
    """«Your service was deleted», once each (queued by the deletion's own transaction)."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ServiceNotice.event_key, PasarGuardAccount, User)
                .join(PasarGuardAccount, PasarGuardAccount.id == ServiceNotice.account_id)
                .join(User, User.id == ServiceNotice.user_id)
                .where(
                    ServiceNotice.kind == NOTICE_DELETED,
                    or_(
                        ServiceNotice.sent_at.is_(None),
                        and_(
                            ServiceNotice.delivered_at.is_(None),
                            ServiceNotice.undelivered.is_(False),
                            ServiceNotice.sent_at < datetime.now(UTC) - STALE_CLAIM,
                        ),
                    ),
                )
                .order_by(ServiceNotice.id)
            )
        ).all()
        pending = [
            (
                str(key),
                int(user.telegram_id),
                service_list_label(account, reseller=bool(user.is_reseller)),
            )
            for key, account, user in rows
        ]
        await end_read_transaction(session)
    sent = 0
    for key, telegram_id, label in pending:
        async with session_factory() as session:
            claimed = await claim_queued(session, event_key=key, now=datetime.now(UTC))
            if not claimed:
                await end_read_transaction(session)
                continue
            await session.commit()
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=texts.notify_service_deleted.format(name=html.escape(label)),
                reply_markup=buy_keyboard(),
            )
        except Exception as exc:
            logger.warning("Lifecycle: deleted notice failed (%s)", type(exc).__name__)
            async with session_factory() as session:
                if is_permanent_send_failure(exc):
                    await mark_delivered(session, event_key=key, refused_for_good=True)
                else:
                    await release_notice(session, event_key=key)
                await session.commit()
            continue
        async with session_factory() as session:
            await mark_delivered(session, event_key=key)
            await session.commit()
        sent += 1
    return sent


# --- one deletion ---------------------------------------------------------------------------------------


async def delete_one(  # noqa: PLR0911 - every refusal is its own named return
    session_factory: async_sessionmaker[Any],
    client: PasarGuardClient,
    account_id: int,
    config: LifecycleConfig,
    now: datetime,
    *,
    bot: Bot,
    allow_delete: bool = True,
) -> Outcome:
    """Re-read the panel, decide again, record the intent, delete, mark. Nothing else deletes.

    Every refusal is a returned ``skipped`` with its reason: the caller names it.
    """
    async with session_factory() as session:
        before = await load_facts(session, account_ids=[account_id])
        await end_read_transaction(session)
    if not before:
        return Outcome(account_id, "", "skipped", "gone")
    pre = before[0]
    title = pre.title

    read, panel_user = await read_panel(client, pre.panel_username)
    if read == READ_ERROR:
        return Outcome(account_id, title, "skipped", "panel_error")
    if read == READ_MISSING:
        # Only the daily run's bookkeeping finishes an intent (it can tell the panel still
        # answers for other users). Here a 404 just keeps the service.
        return Outcome(account_id, title, "skipped", "panel_missing")

    # Phase A: decide on rows read AFTER the panel answered, and commit the intent.
    async with session_factory() as session:
        fresh = await load_facts(session, account_ids=[account_id])
        if not fresh:
            await end_read_transaction(session)
            return Outcome(account_id, title, "skipped", "gone")
        facts = fresh[0]
        decision = decide(facts, panel_user, config, now)
        if decision.verdict == WARN:
            await end_read_transaction(session)
            sent = await send_delete_warning(
                session_factory, bot, facts, panel_user, decision, config, now
            )
            return Outcome(account_id, title, "warned" if sent else "skipped", decision.reason)
        if decision.verdict == DELETE and not allow_delete:
            # Listed only to be warned (no proof of an earlier warning then): never delete it.
            await end_read_transaction(session)
            return Outcome(account_id, title, "skipped", "warned_recently")
        if decision.verdict != DELETE:
            if facts.delete_requested_at is not None:
                await session.execute(
                    update(PasarGuardAccount)
                    .where(PasarGuardAccount.id == account_id)
                    .values(delete_requested_at=None)
                )
                await session.commit()
            else:
                await end_read_transaction(session)
            return Outcome(account_id, title, "skipped", decision.reason)
        result = await session.execute(
            update(PasarGuardAccount)
            .where(PasarGuardAccount.id == account_id, live_account_clause())
            .values(delete_requested_at=now)
        )
        if int(result.rowcount or 0) != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return Outcome(account_id, title, "skipped", "gone")
        await session.commit()  # the intent is durable BEFORE the panel is asked

    # One more look at the rows: an order approved or a change made since the first read must
    # stop the delete. The intent is cleared, never left behind for a later 404 to finish.
    async with session_factory() as session:
        again = await load_facts(session, account_ids=[account_id])
        recheck = decide(again[0], panel_user, config, now) if again else None
        if recheck is None or recheck.verdict != DELETE:
            await session.execute(
                update(PasarGuardAccount)
                .where(PasarGuardAccount.id == account_id)
                .values(delete_requested_at=None)
            )
            await session.commit()
            return Outcome(
                account_id, title, "skipped", recheck.reason if recheck is not None else "gone"
            )
        await end_read_transaction(session)

    # Phase B: the panel. 404 = already gone; any other error leaves the intent and skips.
    try:
        await client.delete_user(facts.panel_username)
    except PasarGuardNotFoundError:
        pass
    except Exception as exc:
        logger.warning(
            "Lifecycle: panel delete failed for %s (%s)", facts.panel_username, type(exc).__name__
        )
        return Outcome(account_id, title, "skipped", "delete_failed")

    # Phase C: one transaction: the soft delete and the customer's queued notice.
    async with session_factory() as session:
        changed = await _soft_delete(session, account_id, now)
        await session.commit()
    if not changed:
        # Someone else (a second process) already marked it: not ours to count or announce.
        return Outcome(account_id, title, "skipped", "gone")
    period = period_of(
        int(getattr(panel_user, "data_limit", 0) or 0), getattr(panel_user, "expire", None)
    )
    note = (
        NOTE_WARNING_UNDELIVERED
        if delete_warning_key(account_id, period) in facts.undelivered
        else ""
    )
    return Outcome(account_id, title, "deleted", decision.reason, note)


# --- reports ------------------------------------------------------------------------------------------------


def _line(title: str, owner: str, reason: str) -> str:
    return texts.admin_life_report_row.format(
        title=html.escape(title), owner=html.escape(owner), reason=reason
    )


def _capped(lines: list[str]) -> str:
    shown = lines[:REPORT_LIST_CAP]
    extra = len(lines) - len(shown)
    if extra > 0:
        shown.append(texts.admin_life_report_more.format(n=_fa(extra)))
    return "\n".join(shown)


def _reason_of(e: Evaluated) -> str:
    return reason_fa(e.decision.kind or "", e.decision.days)


async def _report_rehearsal(
    session: AsyncSession,
    evaluated: Sequence[Evaluated],
    *,
    now: datetime,
    stopped: bool,
    failed: int = 0,
) -> None:
    deletes = [e for e in evaluated if e.decision.verdict == DELETE]
    after = [e for e in evaluated if e.decision.verdict == WARN and e.decision.after_warning]
    warns = [e for e in evaluated if e.decision.verdict == WARN and not e.decision.after_warning]

    def block(title: str, items: list[Evaluated]) -> str:
        if not items:
            return ""
        lines = [_line(e.facts.title, e.facts.owner_label, _reason_of(e)) for e in items]
        return f"\n\n{title.format(n=_fa(len(items)))}\n{_capped(lines)}"

    body = (
        texts.admin_life_report_rehearsal_head
        + block(texts.admin_life_report_would_delete, deletes)
        + block(texts.admin_life_report_would_delete_after, after)
        + block(texts.admin_life_report_would_warn, warns)
    )
    if not (deletes or after or warns):
        body += "\n\n" + texts.admin_life_report_nothing
    if stopped:
        body += "\n\n" + texts.admin_life_report_would_stop
    if failed:
        body += "\n\n" + texts.admin_life_report_reads_failed.format(n=_fa(failed))
    await enqueue_report(
        session,
        event_key=f"lifecycle:rehearsal:{now.date().isoformat()}",
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


async def _report_run(
    session: AsyncSession,
    outcomes: Sequence[Outcome],
    *,
    now: datetime,
    source: str,
) -> None:
    deleted = [o for o in outcomes if o.status == "deleted"]
    other = [o for o in outcomes if o.status != "deleted"]
    if not deleted and not other:
        return
    body = texts.admin_life_report_run_head.format(
        source=source, deleted=_fa(len(deleted)), skipped=_fa(len(other))
    )
    if deleted:
        body += "\n\n" + _capped([deleted_line(o) for o in deleted])
    if other:
        body += (
            "\n\n"
            + texts.admin_life_report_skipped_head
            + "\n"
            + _capped([f"• {html.escape(o.title)} — {skip_reason_fa(o.reason)}" for o in other])
        )
    await enqueue_report(
        session,
        event_key=f"lifecycle:run:{now.isoformat(timespec='microseconds')}",
        topic_key=TOPIC_SYSTEM,
        body=body,
    )


def deleted_line(outcome: Outcome) -> str:
    """«• title», plus «هشدار نرسید» when the customer never received the warning."""
    line = f"• {html.escape(outcome.title)}"
    if outcome.note == NOTE_WARNING_UNDELIVERED:
        line += " — " + texts.admin_life_warning_undelivered
    return line


def skip_reason_fa(code: str) -> str:
    """Why a service was kept, in words the owner reads."""
    table = texts.admin_life_skip_reasons
    if code in table:
        return table[code]
    if code.startswith("status_"):
        return table["status_other"]
    return table["other"]


# --- the daily run ---------------------------------------------------------------------------------------------


def _items_of(evaluated: Sequence[Evaluated]) -> list[ItemData]:
    return [
        ItemData(
            account_id=e.facts.account_id,
            action=ITEM_DELETE if e.decision.verdict == DELETE else ITEM_WARN,
            reason_code=e.decision.kind or "",
            reason_days=e.decision.days,
            after_warning=e.decision.after_warning,
        )
        for e in evaluated
        if e.decision.verdict in (DELETE, WARN)
    ]


async def run_lifecycle(
    *,
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    now: datetime | None = None,
    pause_seconds: float | None = None,
    client: PasarGuardClient | None = None,
) -> LifecycleRunResult:
    """One daily pass. Never raises into the scheduler."""
    result = LifecycleRunResult()
    if _run_lock.locked():
        logger.info("Lifecycle already running; skip this fire")
        result.busy = True
        return result
    async with _run_lock:
        try:
            await _run(
                session_factory,
                bot,
                settings,
                result,
                now or datetime.now(UTC),
                pause_seconds,
                client,
            )
        except Exception:
            logger.exception("service_lifecycle job failed")
    return result


async def _run(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    result: LifecycleRunResult,
    now: datetime,
    pause: float | None,
    client: PasarGuardClient | None,
) -> None:
    async with session_factory() as session:
        config = await load_config(session, seed_days=settings.renewal_reminder_days)
        result.mode = config.delete_mode
        if config.delete_mode not in (DELETE_MODE_REHEARSAL, DELETE_MODE_ON):
            await end_read_transaction(session)
            await send_deleted_notices(session_factory, bot)
            return
        facts_list = await load_facts(session)
        result.services = len(facts_list)
        if not facts_list:
            await end_read_transaction(session)
            return
        own_client = client is None
        panel = client or await get_pasarguard_client(session, settings)
        await end_read_transaction(session)
    try:
        evaluated = await evaluate(panel, facts_list, config, now, pause=pause)
    finally:
        if own_client:
            await panel.aclose()

    on = config.delete_mode == DELETE_MODE_ON
    finished = await _apply_bookkeeping(session_factory, evaluated, now=now, finish=on)
    result.deleted += finished
    await _report_mismatches(session_factory, evaluated, now)
    deletes = [e for e in evaluated if e.decision.verdict == DELETE]
    warns = [e for e in evaluated if e.decision.verdict == WARN and e.panel_user is not None]
    live = result.services - finished
    stopped = hard_stop_reached(len(deletes), live)
    warn_stopped = hard_stop_reached(len(warns), live)

    if not on:
        failed = sum(1 for e in evaluated if e.read == READ_ERROR)
        reads_ok = sum(1 for e in evaluated if e.read == READ_OK)
        async with session_factory() as session:
            await _report_rehearsal(
                session, evaluated, now=now, stopped=stopped or warn_stopped, failed=failed
            )
            # A rehearsal with the panel down proved nothing: it must not unlock «روشن».
            if reads_ok >= 1 and failed * 2 < len(evaluated):
                await record_rehearsal(session, seed_days=settings.renewal_reminder_days, now=now)
            await session.commit()
        return

    stop_items = [*(deletes if stopped else []), *(warns if warn_stopped else [])]
    if stop_items:
        result.hard_stopped = True
        await _store_stop_batch(
            session_factory, config, stop_items, result, now, deletes=stopped, warns=warn_stopped
        )
    if not stopped:
        outcomes = await _delete_all(session_factory, bot, settings, deletes, config, now, client)
        result.outcomes += outcomes
        result.deleted += sum(1 for o in outcomes if o.status == "deleted")
        result.skipped += sum(1 for o in outcomes if o.status == "skipped")
        async with session_factory() as session:
            await _report_run(session, outcomes, now=now, source=texts.admin_life_source_daily)
            await session.commit()

    if not warn_stopped:
        for e in warns:
            # Reload the facts (exemption included) right before each warning goes out.
            if await warn_one(session_factory, bot, e.facts.account_id, e.panel_user, config, now):
                result.warned += 1
    await send_deleted_notices(session_factory, bot)


async def warn_one(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    account_id: int,
    panel_user: Any,
    config: LifecycleConfig,
    now: datetime,
) -> bool:
    """Send this service's deletion warning, but only if the rows still say WARN."""
    async with session_factory() as session:
        fresh = await load_facts(session, account_ids=[account_id])
        await end_read_transaction(session)
    if not fresh:
        return False
    decision = decide(fresh[0], panel_user, config, now)
    if decision.verdict != WARN:
        return False
    return await send_delete_warning(
        session_factory, bot, fresh[0], panel_user, decision, config, now
    )


async def _report_mismatches(
    session_factory: async_sessionmaker[Any], evaluated: Sequence[Evaluated], now: datetime
) -> None:
    """A name that answers as another panel user is never deleted; the owner is told."""
    odd = [e for e in evaluated if e.decision.reason == "panel_id_mismatch"]
    if not odd:
        return
    async with session_factory() as session:
        await enqueue_report(
            session,
            event_key=f"lifecycle:mismatch:{now.date().isoformat()}",
            topic_key=TOPIC_ERRORS,
            body=texts.admin_life_report_id_mismatch.format(n=_fa(len(odd)))
            + "\n"
            + _capped([f"• {html.escape(e.facts.title)}" for e in odd]),
        )
        await session.commit()


async def _delete_all(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    deletes: Sequence[Evaluated],
    config: LifecycleConfig,
    now: datetime,
    client: PasarGuardClient | None,
) -> list[Outcome]:
    if not deletes:
        return []
    own_client = client is None
    async with session_factory() as session:
        panel = client or await get_pasarguard_client(session, settings)
        await end_read_transaction(session)
    outcomes: list[Outcome] = []
    try:
        for index, e in enumerate(deletes):
            if index:
                await asyncio.sleep(DEFAULT_PAUSE_SECONDS)
            try:
                outcomes.append(
                    await delete_one(
                        session_factory, panel, e.facts.account_id, config, now, bot=bot
                    )
                )
            except Exception:
                logger.exception("Lifecycle: delete failed for %s", e.facts.panel_username)
                outcomes.append(Outcome(e.facts.account_id, e.facts.title, "skipped", "error"))
    finally:
        if own_client:
            await panel.aclose()
    return outcomes


async def _store_stop_batch(
    session_factory: async_sessionmaker[Any],
    config: LifecycleConfig,
    items: Sequence[Evaluated],
    result: LifecycleRunResult,
    now: datetime,
    *,
    deletes: bool,
    warns: bool,
) -> None:
    """Delete (or warn) nobody; keep the list for the owner and tell him once.

    ``deletes``/``warns`` say which of the two limits was reached. Warnings have their own
    stop at the same thresholds: a flood of «will be deleted» messages is as hard to take back
    as a deletion.
    """
    ids = [e.facts.account_id for e in items]
    async with session_factory() as session:
        same = await same_open_list(session, config=config, account_ids=ids, now=now)
        if same is not None:
            result.batch_id = int(same.id)
            await end_read_transaction(session)
            return
        batch = await create_batch(
            session, kind=BATCH_KIND_STOP, config=config, items=_items_of(items), now=now
        )
        result.batch_id = int(batch.id)
        n_delete = sum(1 for e in items if e.decision.verdict == DELETE)
        n_warn = len(items) - n_delete
        body = texts.admin_life_report_stop.format(n=_fa(n_delete)) if deletes else ""
        if warns:
            body += ("\n\n" if body else "") + texts.admin_life_report_warn_stop.format(
                n=_fa(n_warn)
            )
        await enqueue_report(
            session, event_key=f"lifecycle:stop:{batch.id}", topic_key=TOPIC_ERRORS, body=body
        )
        await session.commit()


# --- the owner's preview and his approval ------------------------------------------------------------------------------


async def build_preview(
    session_factory: async_sessionmaker[Any],
    client: PasarGuardClient,
    config: LifecycleConfig,
    now: datetime,
    *,
    pause: float | None = None,
) -> int:
    """Today's decisions, read live, stored as a preview list. Deletes and sends nothing.

    Decided as if deleting were on: the owner wants to see what a real run would do.
    """
    async with session_factory() as session:
        facts_list = await load_facts(session)
        await end_read_transaction(session)
    evaluated = await evaluate(
        client, facts_list, replace(config, delete_mode=DELETE_MODE_ON), now, pause=pause
    )
    async with session_factory() as session:
        batch = await create_batch(
            session, kind=BATCH_KIND_PREVIEW, config=config, items=_items_of(evaluated), now=now
        )
        await session.commit()
        return int(batch.id)


async def make_batch_from_preview(
    session_factory: async_sessionmaker[Any],
    preview_id: int,
    config: LifecycleConfig,
    now: datetime,
) -> int:
    """A fresh approvable list from the preview's deletions (the owner's own act).

    Every id is still re-read and re-decided at approval, so an item that stopped
    qualifying since the preview is skipped there.
    """
    from database.models.lifecycle import LifecycleBatchItem

    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    select(LifecycleBatchItem).where(
                        LifecycleBatchItem.batch_id == preview_id,
                        LifecycleBatchItem.action == ITEM_DELETE,
                    )
                )
            )
            .scalars()
            .all()
        )
        items = [
            ItemData(
                account_id=int(r.account_id),
                action=ITEM_DELETE,
                reason_code=r.reason_code,
                reason_days=r.reason_days,
            )
            for r in stored
        ]
        batch = await create_batch(
            session, kind=BATCH_KIND_MANUAL, config=config, items=items, now=now
        )
        await session.commit()
        return int(batch.id)


@dataclass
class ApproveResult:
    deleted: list[Outcome] = field(default_factory=list)
    skipped: list[Outcome] = field(default_factory=list)
    warned: list[Outcome] = field(default_factory=list)


async def approve_batch(
    session_factory: async_sessionmaker[Any],
    bot: Bot,
    settings: Settings,
    batch_id: int,
    *,
    now: datetime | None = None,
    client: PasarGuardClient | None = None,
) -> ApproveResult:
    """Delete ONLY the ids of this list, each re-read and re-decided right before.

    The hard stop does not apply (the approval is the check); every other guard does,
    warn-first included. The list is marked used BEFORE the first intent, in its own
    committed transaction, so a second confirm deletes nothing more.
    """
    moment = now or datetime.now(UTC)
    if _run_lock.locked():
        raise BatchRefused("busy")
    async with _run_lock:
        async with session_factory() as session:
            config = await load_config(session, seed_days=settings.renewal_reminder_days)
            batch = await get_batch(session, batch_id)
            if config.delete_mode != DELETE_MODE_ON:
                raise BatchRefused("mode")
            if batch is None:
                raise BatchRefused("missing")
            problem = void_reason(batch, config, moment)
            if problem is not None:
                raise BatchRefused(problem)
            items = await batch_items(session, batch_id)
            if not await claim_batch(session, batch_id, now=moment):
                await session.rollback()
                raise BatchRefused("used")
            await session.commit()
            own_client = client is None
            panel = client or await get_pasarguard_client(session, settings)
            await end_read_transaction(session)
        result = ApproveResult()
        try:
            for index, (account_id, action) in enumerate(items):
                if index:
                    await asyncio.sleep(DEFAULT_PAUSE_SECONDS)
                try:
                    outcome = await delete_one(
                        session_factory,
                        panel,
                        account_id,
                        config,
                        moment,
                        bot=bot,
                        allow_delete=action == ITEM_DELETE,
                    )
                except Exception:
                    logger.exception("Lifecycle: approved delete failed for %s", account_id)
                    outcome = Outcome(account_id, "", "skipped", "error")
                if outcome.status == "deleted":
                    result.deleted.append(outcome)
                elif outcome.status == "warned":
                    result.warned.append(outcome)
                else:
                    result.skipped.append(outcome)
        finally:
            if own_client:
                await panel.aclose()
        async with session_factory() as session:
            await _report_run(
                session,
                [*result.deleted, *result.skipped, *result.warned],
                now=moment,
                source=texts.admin_life_source_approved,
            )
            await session.commit()
        await send_deleted_notices(session_factory, bot)
        return result


__all__ = [
    "ApproveResult",
    "BatchRefused",
    "LifecycleRunResult",
    "Outcome",
    "approve_batch",
    "build_preview",
    "delete_one",
    "make_batch_from_preview",
    "run_lifecycle",
    "skip_reason_fa",
]
