"""Reserve an exact payable amount; never recycle it onto another order."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from database.models import Order, OrderStatus
from database.models.bank_sms import BankInvoice
from services.payment_settings import get_payment_info, normalize_card_number

BANK_SMS_PATH = "/bank-sms/blu"
INVOICE_LIFETIME = timedelta(minutes=30)


def require_bank_sms_config(settings: Settings) -> None:
    if not settings.bank_sms_enabled:
        return
    parts = urlsplit(settings.bank_sms_url)
    if (
        len(settings.bank_sms_token) < 32
        or not settings.bank_sms_sender.strip()
        or not settings.bank_sms_account_key.strip()
        or len(settings.bank_sms_account_key) > 64
        or parts.scheme != "https"
        or not parts.hostname
        or parts.path != BANK_SMS_PATH
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or not settings.scheduler_enabled
    ):
        raise RuntimeError(
            "BANK_SMS requires a 32+ character token, sender, stable account key, HTTPS URL ending /bank-sms/blu and enabled scheduler"
        )


def card_digest(card_number: str) -> str:
    return sha256(normalize_card_number(card_number).encode("ascii")).hexdigest()


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


async def card_due_toman(session: AsyncSession, order: Order) -> int:
    """The frozen invoice amount, including suffix, or legacy checkout due."""
    invoice = await session.get(BankInvoice, order.id)
    if invoice is not None:
        return int(invoice.amount_toman)
    return max(0, int(order.price_toman) - int(order.wallet_applied_toman or 0))


async def reserve_bank_invoice(  # noqa: PLR0911 - explicit fail-closed reservation gates
    session: AsyncSession, settings: Settings, *, order_id: int, now: datetime | None = None
) -> BankInvoice | None:
    existing = await session.get(BankInvoice, order_id)
    if existing is not None or not settings.bank_sms_enabled:
        return existing
    require_bank_sms_config(settings)
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None or order.status != OrderStatus.PENDING_PAYMENT:
        return None
    # Recheck after the row lock: concurrent resumes of this same checkout.
    existing = await session.get(BankInvoice, order_id, populate_existing=True)
    if existing is not None:
        return existing
    due = max(0, int(order.price_toman) - int(order.wallet_applied_toman or 0))
    if due <= 0:
        return None
    info = await get_payment_info(session, settings)
    digest = card_digest(info.card_number)
    moment = aware(now or datetime.now(UTC))
    lifetime = INVOICE_LIFETIME
    if settings.order_expiry_minutes > 0:
        lifetime = min(lifetime, timedelta(minutes=settings.order_expiry_minutes))
    expires_at = aware(order.created_at) + lifetime
    if moment >= expires_at:
        return None
    # Permanent full-amount uniqueness also prevents collisions across nearby
    # prices. Exhaustion falls back to the ordinary manual receipt flow.
    used = set(
        (
            await session.scalars(
                select(BankInvoice.amount_toman).where(
                    BankInvoice.account_key == settings.bank_sms_account_key,
                    BankInvoice.amount_toman.between(due + 1, due + 999),
                )
            )
        ).all()
    )
    for offset in range(999):
        suffix = (order_id + offset) % 999 + 1
        if due + suffix in used:
            continue
        invoice = BankInvoice(
            order_id=order_id,
            account_key=settings.bank_sms_account_key,
            card_digest=digest,
            base_due_toman=due,
            suffix_toman=suffix,
            amount_toman=due + suffix,
            created_at=moment,
            expires_at=expires_at,
        )
        try:
            async with session.begin_nested():
                session.add(invoice)
                await session.flush()
        except IntegrityError:
            existing = await session.get(BankInvoice, order_id)
            if existing is not None:
                return existing
            continue
        return invoice
    return None
