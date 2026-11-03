"""Customer wallet: balance + immutable ledger (credit/debit)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User, WalletEntryType, WalletLedgerEntry

REASON_PROVISION_FAIL_CREDIT = "provision_fail_credit"
REASON_CHECKOUT_DEBIT = "checkout_debit"
REASON_RETRY_DEBIT = "retry_debit"
REASON_ADMIN_ADJUST = "admin_adjust"

# Every reason a ledger row can carry, as the owner reads it. A screen that
# lists the ledger goes through ``wallet_reason_label``; a code with no entry
# reads «سایر», never its raw key («checkout_debit» reached the customer card).
WALLET_REASON_FA: dict[str, str] = {
    "admin_manual_credit": "شارژ دستی ادمین",
    "admin_manual_debit": "کسر دستی ادمین",
    REASON_ADMIN_ADJUST: "اصلاح دستی ادمین",
    REASON_CHECKOUT_DEBIT: "خرید با کیف پول",
    "checkout_refund": "برگشت پول خرید",
    REASON_RETRY_DEBIT: "کسر برای فعال‌سازی دوباره",
    REASON_PROVISION_FAIL_CREDIT: "جبران فعال‌سازی ناموفق",
    "purchase": "خرید",
    "renewal": "تمدید",
    "referral_reward": "پاداش معرفی",
    "reseller_trial": "تست نمایندگی",
    "topup": "شارژ",
    "bank_payment_suffix": "برگشت خرده‌مبلغ پرداخت",
    "migration_opening": "ماندهٔ انتقال میرزا",
}
WALLET_REASON_OTHER_FA = "سایر"


def wallet_reason_label(reason: str | None) -> str:
    """The Persian label of a ledger reason code; never the code itself."""
    return WALLET_REASON_FA.get(str(reason or ""), WALLET_REASON_OTHER_FA)


class WalletError(Exception):
    """Business-rule failure for wallet operations."""


class InsufficientWalletFunds(WalletError):
    """Debit requested more than available balance."""


def provision_fail_credit_key(order_id: int) -> str:
    return f"provision_fail_credit:{order_id}"


def checkout_debit_key(order_id: int) -> str:
    return f"checkout_debit:{order_id}"


def retry_debit_key(order_id: int) -> str:
    return f"retry_debit:{order_id}"


async def get_balance(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(select(User.wallet_balance_toman).where(User.id == user_id))
    balance = result.scalar_one_or_none()
    if balance is None:
        raise WalletError("user_not_found")
    return int(balance)


async def _load_user_for_update(session: AsyncSession, user_id: int) -> User:
    """Load user row with a real row lock on Postgres (``SELECT … FOR UPDATE``).

    SQLite ignores ``FOR UPDATE``; tests may approximate. Design target is PostgreSQL.
    Prefer calling inside an open transaction before balance mutation.
    """
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if user is None:
        raise WalletError("user_not_found")
    return user


async def _get_by_idempotency(
    session: AsyncSession, idempotency_key: str
) -> WalletLedgerEntry | None:
    result = await session.execute(
        select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def checkout_balance_window(session: AsyncSession, order_id: int) -> tuple[int, int] | None:
    """Wallet before and after the debit that paid this order, if one exists.

    The sale card shows these two numbers. A card-to-card payment has no debit,
    and inventing a before/after from the current balance would be a lie.
    """
    result = await session.execute(
        select(WalletLedgerEntry)
        .where(
            WalletLedgerEntry.order_id == order_id,
            WalletLedgerEntry.reason.in_((REASON_CHECKOUT_DEBIT, REASON_RETRY_DEBIT)),
        )
        .order_by(WalletLedgerEntry.id.desc())
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return None
    after = int(entry.balance_after_toman)
    return after + int(entry.amount_toman), after


async def _report_ledger_entry(session: AsyncSession, entry: WalletLedgerEntry) -> None:
    """Queue a wallet report inside the caller's transaction.

    Imported lazily so the wallet keeps working when reporting is not set up,
    and enqueued (never sent) so a report failure cannot roll back money.

    A checkout debit is the sale itself. The sales card already shows the
    balance move, and a second «از کیف پول کم شد» would read as an admin taking
    money.
    """
    if str(entry.reason) in {REASON_CHECKOUT_DEBIT, REASON_RETRY_DEBIT}:
        return
    from database.models import User
    from services.report_events import wallet_changed
    from services.report_outbox import enqueue_report

    user = await session.get(User, int(entry.user_id))
    if user is None:
        return
    signed = int(entry.amount_toman)
    if entry.entry_type is WalletEntryType.debit:
        signed = -signed
    report = wallet_changed(
        entry_id=int(entry.id),
        telegram_id=int(user.telegram_id),
        name=user.first_name or user.username,
        username=user.username,
        amount_toman=signed,
        balance_after=int(entry.balance_after_toman),
        reason=str(entry.reason),
        idempotency_key=str(entry.idempotency_key),
        when=entry.created_at,
        link_user_id=int(user.id),
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=report.link_user_id,
    )


async def credit(
    session: AsyncSession,
    *,
    user_id: int,
    amount_toman: int,
    reason: str,
    idempotency_key: str,
    order_id: int | None = None,
    note: str | None = None,
) -> tuple[WalletLedgerEntry, bool]:
    """Credit wallet. Returns (entry, created). Idempotent on ``idempotency_key``."""
    if amount_toman <= 0:
        raise WalletError("invalid_amount")

    existing = await _get_by_idempotency(session, idempotency_key)
    if existing is not None:
        return existing, False

    user = await _load_user_for_update(session, user_id)
    new_balance = int(user.wallet_balance_toman) + int(amount_toman)
    entry = WalletLedgerEntry(
        user_id=user_id,
        entry_type=WalletEntryType.credit,
        amount_toman=int(amount_toman),
        reason=reason,
        order_id=order_id,
        idempotency_key=idempotency_key,
        note=note,
        balance_after_toman=new_balance,
    )
    user.wallet_balance_toman = new_balance
    session.add(entry)
    await session.flush()
    await _report_ledger_entry(session, entry)
    return entry, True


async def debit(
    session: AsyncSession,
    *,
    user_id: int,
    amount_toman: int,
    reason: str,
    idempotency_key: str,
    order_id: int | None = None,
    note: str | None = None,
) -> tuple[WalletLedgerEntry, bool]:
    """Debit wallet. Fails with InsufficientWalletFunds if balance too low.

    Idempotent on ``idempotency_key`` (repeat returns existing entry).
    """
    if amount_toman <= 0:
        raise WalletError("invalid_amount")

    existing = await _get_by_idempotency(session, idempotency_key)
    if existing is not None:
        return existing, False

    user = await _load_user_for_update(session, user_id)
    current = int(user.wallet_balance_toman)
    need = int(amount_toman)
    if current < need:
        raise InsufficientWalletFunds(f"insufficient_funds:have={current}:need={need}")

    # Conditional update suitable for Postgres: refuses to go negative under races.
    from sqlalchemy import update

    upd = await session.execute(
        update(User)
        .where(User.id == user_id, User.wallet_balance_toman >= need)
        .values(wallet_balance_toman=User.wallet_balance_toman - need)
        .returning(User.wallet_balance_toman)
    )
    row = upd.first()
    if row is None:
        # Re-read under lock for accurate error (should be rare after FOR UPDATE).
        await session.refresh(user)
        raise InsufficientWalletFunds(
            f"insufficient_funds:have={int(user.wallet_balance_toman)}:need={need}"
        )
    new_balance = int(row[0])
    user.wallet_balance_toman = new_balance
    entry = WalletLedgerEntry(
        user_id=user_id,
        entry_type=WalletEntryType.debit,
        amount_toman=need,
        reason=reason,
        order_id=order_id,
        idempotency_key=idempotency_key,
        note=note,
        balance_after_toman=new_balance,
    )
    session.add(entry)
    await session.flush()
    await _report_ledger_entry(session, entry)
    return entry, True


async def list_recent_entries(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 20,
) -> list[WalletLedgerEntry]:
    result = await session.execute(
        select(WalletLedgerEntry)
        .where(WalletLedgerEntry.user_id == user_id)
        .order_by(WalletLedgerEntry.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    return list(result.scalars().all())


async def has_provision_fail_credit(session: AsyncSession, order_id: int) -> bool:
    result = await session.execute(
        select(WalletLedgerEntry.id).where(
            WalletLedgerEntry.idempotency_key == provision_fail_credit_key(order_id)
        )
    )
    return result.scalar_one_or_none() is not None


async def has_valid_retry_debit(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    amount_toman: int,
) -> bool:
    """Return whether the refund was repaid by this order's retry debit."""
    result = await session.execute(
        select(WalletLedgerEntry.id).where(
            WalletLedgerEntry.idempotency_key == retry_debit_key(order_id),
            WalletLedgerEntry.entry_type == WalletEntryType.debit,
            WalletLedgerEntry.reason == REASON_RETRY_DEBIT,
            WalletLedgerEntry.user_id == user_id,
            WalletLedgerEntry.order_id == order_id,
            WalletLedgerEntry.amount_toman == amount_toman,
        )
    )
    return result.scalar_one_or_none() is not None


async def credit_provision_failed(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    amount_toman: int,
    max_n: int = 1,
) -> tuple[WalletLedgerEntry | None, bool]:
    """Credit wallet on PROVISION_FAILED (N=1 by default). Idempotent.

    ``max_n`` reserved for config; current policy only supports N=1 via unique key.
    """
    if max_n < 1:
        return None, False
    if amount_toman <= 0:
        return None, False
    return await credit(
        session,
        user_id=user_id,
        amount_toman=amount_toman,
        reason=REASON_PROVISION_FAIL_CREDIT,
        idempotency_key=provision_fail_credit_key(order_id),
        order_id=order_id,
        note="provision_failed_refund",
    )
