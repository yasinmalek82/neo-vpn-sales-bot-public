"""Strict Blu SMS parsing and transactional, fail-closed payment matching."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import jdatetime
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from database.models import Order, OrderStatus
from database.models.bank_sms import BankInvoice, BankSmsEvent
from services.bank_invoices import (
    aware,
    card_digest,
    manual_card_amounts,
    require_bank_sms_config,
    reserve_bank_invoice,
)
from services.finance import has_valid_checkout_debit_for_order
from services.payment_settings import PaymentSettingsError, bank_sms_auto_enabled, get_payment_info
from services.payments import approve_bank_notification
from services.report_events import bank_sms_manual_review
from services.report_outbox import enqueue_report

__all__ = ["BankSmsError", "parse_blu_sms", "receive_bank_sms", "reserve_bank_invoice"]

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬", "01234567890123456789,")
_MONEY = r"(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*)"
_BLU = re.compile(
    r"بلو\nواریز پول\n[^\n،]{1,80} عزیز، (?P<amount>" + _MONEY + r")\s*ریال به حساب شما نشست\.\n"
    r"موجودی:\s*(?:" + _MONEY + r"|0)\s*ریال\n"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})\n"
    r"(?P<year>[0-9]{4})\.(?P<month>[0-9]{2})\.(?P<day>[0-9]{2})"
)


class BankSmsError(ValueError):
    """Unsupported or untrusted input. Never include SMS text in the error."""


@dataclass(frozen=True)
class BankCredit:
    amount_toman: int
    occurred_at: datetime


@dataclass(frozen=True)
class BankSmsResult:
    outcome: str


def parse_blu_sms(text: str) -> BankCredit:
    if len(text) > 2048:
        raise BankSmsError("invalid_sms")
    normalized = "\n".join(line.strip() for line in text.translate(_DIGITS).strip().splitlines())
    match = _BLU.fullmatch(normalized)
    if match is None:
        raise BankSmsError("unsupported_blu_format")
    rial = int(match["amount"].replace(",", ""))
    if rial % 10 or not 0 < rial // 10 < 2**63:
        raise BankSmsError("invalid_amount")
    try:
        occurred = jdatetime.datetime(
            *(int(match[key]) for key in ("year", "month", "day", "hour", "minute"))
        ).togregorian()
        occurred = occurred.replace(tzinfo=ZoneInfo("Asia/Tehran")).astimezone(UTC)
    except ValueError as exc:
        raise BankSmsError("invalid_bank_time") from exc
    return BankCredit(amount_toman=rial // 10, occurred_at=occurred)


async def receive_bank_sms(
    session: AsyncSession,
    settings: Settings,
    *,
    text: str,
    sender: str,
    now: datetime | None = None,
) -> BankSmsResult:
    if not settings.bank_sms_enabled:
        raise BankSmsError("disabled")
    require_bank_sms_config(settings)
    if sender != settings.bank_sms_sender:
        raise BankSmsError("invalid_sender")
    credit = parse_blu_sms(text)
    moment = aware(now or datetime.now(UTC))
    # Minute precision and no reference number: deduplicate conservatively on
    # account + amount + bank minute, irrespective of balance/name/formatting.
    key = sha256(
        f"{settings.bank_sms_account_key}|{credit.amount_toman}|{credit.occurred_at.isoformat()}".encode()
    ).hexdigest()
    event = BankSmsEvent(
        event_key=key,
        account_key=settings.bank_sms_account_key,
        amount_toman=credit.amount_toman,
        occurred_at=credit.occurred_at,
        received_at=moment,
        outcome="manual_review",
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        return BankSmsResult("duplicate")
    invoice = await session.scalar(
        select(BankInvoice).where(
            BankInvoice.account_key == settings.bank_sms_account_key,
            BankInvoice.amount_toman == credit.amount_toman,
        )
    )
    if invoice is not None:
        event.order_id = invoice.order_id
    if (
        invoice is not None
        and moment - timedelta(hours=24) <= credit.occurred_at <= moment + timedelta(minutes=1)
        and await bank_sms_auto_enabled(session, settings, lock=True)
    ):
        order = await session.scalar(
            select(Order)
            .where(Order.id == invoice.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        info = await get_payment_info(session, settings)
        try:
            current_card_digest = card_digest(info.card_number)
        except PaymentSettingsError:
            current_card_digest = None
        ambiguous = await session.scalar(
            manual_card_amounts()
            .where(
                Order.price_toman - Order.wallet_applied_toman == credit.amount_toman,
                Order.created_at < credit.occurred_at + timedelta(minutes=1),
            )
            .limit(1)
        )
        base_due = (
            max(0, int(order.price_toman) - int(order.wallet_applied_toman or 0)) if order else 0
        )
        if (
            order is not None
            and order.status in {OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_REVIEW}
            and aware(invoice.created_at).replace(second=0, microsecond=0)
            <= credit.occurred_at
            < aware(invoice.expires_at)
            and moment < aware(invoice.expires_at)
            and invoice.card_digest == current_card_digest
            and ambiguous is None
            and base_due == invoice.base_due_toman
            and await has_valid_checkout_debit_for_order(session, order)
        ):
            event.outcome = "matched"
            await session.flush()
            await approve_bank_notification(session, order_id=order.id, event_key=key)
            event.outcome = "approved"
            return BankSmsResult("approved")
    report = bank_sms_manual_review(
        event_key=key, amount_toman=credit.amount_toman, order_id=event.order_id
    )
    await enqueue_report(
        session, event_key=report.event_key, topic_key=report.topic_key, body=report.body
    )
    return BankSmsResult("manual_review")
