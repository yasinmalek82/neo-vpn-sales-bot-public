"""Persistent wallet-only checkout settlement after terminal panel rejection."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardAuthError, PasarGuardNotFoundError
from bot.handlers import purchase
from bot.keyboards.customer import provision_failed_keyboard
from bot.texts import texts
from database.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    User,
    WalletEntryType,
    WalletLedgerEntry,
)
from database.models.plan import DataLimitResetStrategy
from database.models.report import ReportOutbox
from services import finance, provisioning
from services.orders import create_pending_order
from services.payments import approve_wallet_payment
from services.pricing import PlanType, PriceResult
from services.provisioning import ProvisionError, provision_order
from services.ux_format import to_persian_digits
from services.wallet import REASON_CHECKOUT_DEBIT, debit, get_balance

PRICE_TOMAN = 150_000


class _State:
    def __init__(self) -> None:
        self.cleared = 0
        self.data = {
            "plan_type": PlanType.VOLUME.value,
            "volume_gb": 10,
            "duration_days": 30,
            "wallet_applied": PRICE_TOMAN,
        }

    async def get_data(self) -> dict[str, object]:
        return self.data

    async def update_data(self, **values: object) -> dict[str, object]:
        self.data.update(values)
        return dict(self.data)

    async def clear(self) -> None:
        self.cleared += 1


def _panel_user(username: str) -> UserResponse:
    return UserResponse(
        id=71,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.invalid/sub/{username}",
        data_limit=10 * 1024**3,
    )


async def _seed_wallet_checkout(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    async with factory() as session:
        user = User(
            telegram_id=8801,
            username="wallet_customer",
            first_name="Wallet",
            last_name=None,
            language_code="fa",
            referral_code="WALLET01",
            is_admin=False,
            wallet_balance_toman=PRICE_TOMAN,
        )
        plan = Plan(
            name="wallet-settlement-10gb-30d",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=PRICE_TOMAN,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        order = await create_pending_order(
            session,
            user_id=user.id,
            plan=plan,
            price=PriceResult(
                original_price=PRICE_TOMAN,
                discount_amount=0,
                final_price=PRICE_TOMAN,
                currency="IRT",
                meta={"volume_gb": 10, "duration_days": 30},
            ),
            wallet_applied_toman=PRICE_TOMAN,
        )
        await debit(
            session,
            user_id=user.id,
            amount_toman=PRICE_TOMAN,
            reason=REASON_CHECKOUT_DEBIT,
            idempotency_key=f"checkout_debit:{order.id}",
            order_id=order.id,
        )
        await approve_wallet_payment(session, order_id=order.id, user_id=user.id)
        await session.commit()
        return int(user.id), int(plan.id), int(order.id)


async def _run_confirm_wallet_checkout(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncMock,
) -> tuple[int, _State, SimpleNamespace, AsyncMock]:
    """Run the real wallet checkout with only catalog/UI edges substituted."""
    user = User(
        telegram_id=8811,
        username="confirm_wallet",
        first_name="Confirm",
        last_name=None,
        language_code="fa",
        referral_code="CONFIRM1",
        is_admin=False,
        wallet_balance_toman=PRICE_TOMAN,
    )
    plan = Plan(
        name="confirm-wallet-plan",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=PRICE_TOMAN,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add_all([user, plan])
    await session.commit()
    user_id = int(user.id)
    state = _State()
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    settings = SimpleNamespace(max_discount_percent=100, wallet_provision_fail_credit_max_n=1)
    price = PriceResult(
        original_price=PRICE_TOMAN,
        discount_amount=0,
        final_price=PRICE_TOMAN,
        currency="IRT",
        meta={"volume_gb": 10, "duration_days": 30},
    )

    async def no_open_order(*_args: object, **_kwargs: object) -> bool:
        return False

    async def fixed_plan(*_args: object, **_kwargs: object) -> Plan:
        return plan

    delivery = AsyncMock()
    monkeypatch.setattr(purchase, "resume_open_order_if_any", no_open_order)
    monkeypatch.setattr(purchase, "_cfg", AsyncMock(return_value={"default_hwid_limit_volume": 1}))
    monkeypatch.setattr("services.checkout.resolve_price", AsyncMock(return_value=price))
    monkeypatch.setattr(purchase, "resolve_groups_for_plan_type", AsyncMock(return_value=[1]))
    monkeypatch.setattr(purchase, "get_or_create_dynamic_plan", fixed_plan)
    monkeypatch.setattr(purchase, "get_pasarguard_client", AsyncMock(return_value=client))
    monkeypatch.setattr(purchase, "deliver_provisioned_service_card", delivery)
    await purchase.confirm_order(callback, state, session, settings, user)
    return user_id, state, message, delivery


@pytest.mark.asyncio
async def test_terminal_wallet_rejection_refunds_once_with_literal_checkout_amount(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """Two settlement attempts cannot mint a second wallet credit."""
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        first = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("invalid group", status_code=422),
            max_n=1,
        )
        second = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("invalid group", status_code=422),
            max_n=1,
        )
        await session.commit()

    async with factory() as session:
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order_id)
                )
            ).scalars()
        )
        assert first == PRICE_TOMAN
        assert second == 0
        assert [
            entry.amount_toman for entry in entries if entry.entry_type is WalletEntryType.credit
        ] == [PRICE_TOMAN]
        assert await get_balance(session, user_id) == PRICE_TOMAN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_exception",
    [
        PasarGuardAPIError("server error", status_code=500),
        PasarGuardAPIError("conflict", status_code=409),
        PasarGuardAPIError("not found", status_code=404),
        PasarGuardAuthError("forbidden", status_code=403),
        TimeoutError("reply lost"),
        httpx.TransportError("connection lost"),
        ProvisionError("retry_payment_required"),
        RuntimeError("local report failure"),
    ],
)
async def test_unknown_or_local_failure_never_mints_wallet_credit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    original_exception: Exception,
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=original_exception,
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_exception",
    [
        PasarGuardAPIError("bad request", status_code=400),
        PasarGuardAPIError("invalid group", status_code=422),
        PasarGuardAuthError("expired key", status_code=401),
        PasarGuardNotFoundError("service deleted on the panel", status_code=404),
    ],
)
async def test_exact_terminal_panel_rejections_credit_once(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    original_exception: Exception,
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=original_exception,
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == PRICE_TOMAN
        assert await get_balance(session, user_id) == PRICE_TOMAN


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", ["debit", "payment"])
async def test_missing_verified_wallet_payment_or_literal_debit_mints_nothing(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], remove: str
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        if remove == "debit":
            entry = await session.scalar(
                select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order_id)
            )
            assert entry is not None
            await session.delete(entry)
        else:
            payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
            assert payment is not None
            await session.delete(payment)
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAuthError("expired key", status_code=401),
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        disabled = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAuthError("expired key", status_code=401),
            max_n=0,
        )
        await session.commit()
    assert disabled == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_type", WalletEntryType.credit),
        ("reason", "forged_checkout_debit"),
        ("user_id", "other_user"),
        ("order_id", None),
        ("amount_toman", PRICE_TOMAN - 1),
        ("idempotency_key", "checkout_debit:forged"),
    ],
)
async def test_forged_checkout_debit_fields_never_authorize_credit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], field: str, value: object
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        entry = await session.scalar(
            select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order_id)
        )
        order = await session.get(Order, order_id)
        assert entry is not None and order is not None
        if value == "other_user":
            other = User(
                telegram_id=8899,
                username="other_wallet_customer",
                first_name="Other",
                last_name=None,
                language_code="fa",
                referral_code="OTHERWLT",
                is_admin=False,
            )
            session.add(other)
            await session.flush()
            setattr(entry, field, other.id)
        else:
            setattr(entry, field, value)
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("bad request", status_code=400),
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "card_to_card"),
        ("status", PaymentStatus.pending),
        ("user_id", "other_user"),
    ],
)
async def test_unverified_or_nonwallet_payment_never_authorizes_credit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], field: str, value: object
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        order = await session.get(Order, order_id)
        assert payment is not None and order is not None
        if value == "other_user":
            other = User(
                telegram_id=8898,
                username="other_payment_customer",
                first_name="Other",
                last_name=None,
                language_code="fa",
                referral_code="OTHERPAY",
                is_admin=False,
            )
            session.add(other)
            await session.flush()
            setattr(payment, field, other.id)
        else:
            setattr(payment, field, value)
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("bad request", status_code=400),
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_disabled_credit_policy_never_mints_even_for_proven_terminal_rejection(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("bad request", status_code=400),
            max_n=0,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_wallet_full_cover_mismatch_never_authorizes_credit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = OrderStatus.PROVISION_FAILED
        order.wallet_applied_toman = PRICE_TOMAN - 1
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=PasarGuardAPIError("bad request", status_code=400),
            max_n=1,
        )
        await session.commit()
    async with factory() as session:
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_local_finalization_failure_after_panel_success_never_refunds(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local failure after an accepted create is not proof that the panel rejected it."""
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    monkeypatch.setattr(
        provisioning,
        "_finalize_active",
        AsyncMock(side_effect=RuntimeError("local report persistence failed")),
    )
    async with factory() as session:
        with pytest.raises(RuntimeError, match="local report persistence failed") as failure:
            await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        amount = await finance.settle_wallet_provision_failure(
            session,
            order_id=order_id,
            original_exception=failure.value,
            max_n=1,
        )
        await session.rollback()
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.PROVISIONING
        assert amount == 0
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_successful_wallet_checkout_stays_active_without_refund(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    async with factory() as session:
        await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        await session.commit()
    async with factory() as session:
        order = await session.get(Order, order_id)
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order_id)
                )
            ).scalars()
        )
        assert order is not None and order.status is OrderStatus.ACTIVE
        assert [entry.entry_type for entry in entries] == [WalletEntryType.debit]
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_confirm_order_terminal_rejection_persists_refund_and_failure_message(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual wallet checkout keeps debit/payment/order and settles before UI work."""
    factory, _url = two_sessions
    async with factory() as session:
        user = User(
            telegram_id=8811,
            username="confirm_wallet",
            first_name="Confirm",
            last_name=None,
            language_code="fa",
            referral_code="CONFIRM1",
            is_admin=False,
            wallet_balance_toman=PRICE_TOMAN,
        )
        plan = Plan(
            name="confirm-wallet-plan",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=PRICE_TOMAN,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=1,
            is_active=True,
        )
        session.add_all([user, plan])
        await session.commit()
        user_id = int(user.id)

        client = AsyncMock()
        client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
        client.create_user = AsyncMock(
            side_effect=PasarGuardAPIError("invalid group", status_code=422)
        )
        client.aclose = AsyncMock()
        client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
        state = _State()
        message = SimpleNamespace(answer=AsyncMock())
        callback = SimpleNamespace(answer=AsyncMock(), message=message)
        settings = SimpleNamespace(
            max_discount_percent=100,
            wallet_provision_fail_credit_max_n=1,
        )
        price = PriceResult(
            original_price=PRICE_TOMAN,
            discount_amount=0,
            final_price=PRICE_TOMAN,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        )

        async def no_open_order(*_args: object, **_kwargs: object) -> bool:
            return False

        async def fixed_plan(*_args: object, **_kwargs: object) -> Plan:
            return plan

        monkeypatch.setattr(purchase, "resume_open_order_if_any", no_open_order)
        monkeypatch.setattr(
            purchase, "_cfg", AsyncMock(return_value={"default_hwid_limit_volume": 1})
        )
        monkeypatch.setattr("services.checkout.resolve_price", AsyncMock(return_value=price))
        monkeypatch.setattr(purchase, "resolve_groups_for_plan_type", AsyncMock(return_value=[1]))
        monkeypatch.setattr(purchase, "get_or_create_dynamic_plan", fixed_plan)
        monkeypatch.setattr(purchase, "get_pasarguard_client", AsyncMock(return_value=client))

        await purchase.confirm_order(callback, state, session, settings, user)
        assert state.cleared == 1
        client.aclose.assert_awaited_once()
        persisted_order = await session.scalar(select(Order).where(Order.user_id == user_id))
        assert persisted_order is not None
        refund_message = texts.payment_provision_failed_user.format(
            order_id=to_persian_digits(persisted_order.id),
            amount=purchase._fmt_toman(PRICE_TOMAN),
        )
        refund_call = next(
            call for call in message.answer.await_args_list if call.args[0] == refund_message
        )
        assert refund_call.kwargs["reply_markup"] == provision_failed_keyboard()

    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.user_id == user_id))
        assert order is not None
        payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order.id)
                )
            ).scalars()
        )
        assert order.status is OrderStatus.PROVISION_FAILED
        assert (
            payment is not None
            and payment.status is PaymentStatus.verified
            and payment.method == "wallet"
        )
        assert [(entry.entry_type, entry.amount_toman) for entry in entries] == [
            (WalletEntryType.debit, PRICE_TOMAN),
            (WalletEntryType.credit, PRICE_TOMAN),
        ]
        assert await get_balance(session, user_id) == PRICE_TOMAN


async def _assert_told_paid_and_pending(
    session: AsyncSession, user_id: int, message: SimpleNamespace
) -> None:
    """The customer is told the money was taken and the order is waiting, not «ثبت سفارش نشد»."""
    order = await session.scalar(select(Order).where(Order.user_id == user_id))
    assert order is not None
    pending = texts.purchase_paid_provisioning_pending.format(
        order_id=to_persian_digits(order.id), amount=purchase._fmt_toman(PRICE_TOMAN)
    )
    sent = [call.args[0] for call in message.answer.await_args_list]
    assert pending in sent
    assert texts.purchase_error not in sent
    report = await session.scalar(
        select(ReportOutbox).where(
            ReportOutbox.event_key == f"order:{order.id}:provision_unconfirmed"
        )
    )
    assert report is not None, "the owner was never told"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        PasarGuardAPIError("server error", status_code=500),
        TimeoutError("reply lost after panel accept"),
    ],
)
async def test_confirm_order_unknown_failure_keeps_durable_provisioning_without_refund(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    factory, _url = two_sessions
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=failure)
    client.aclose = AsyncMock()
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    async with factory() as session:
        user_id, state, message, _delivery = await _run_confirm_wallet_checkout(
            session, monkeypatch, client
        )
        assert state.cleared == 1
        await _assert_told_paid_and_pending(session, user_id, message)
        assert not any(
            "به کیف پولت برگشت" in call.args[0] for call in message.answer.await_args_list
        )
        client.aclose.assert_awaited_once()

    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.user_id == user_id))
        assert order is not None
        payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order.id)
                )
            ).scalars()
        )
        assert order.status is OrderStatus.PROVISIONING
        assert order.panel_username
        assert payment is not None and payment.status is PaymentStatus.verified
        assert [entry.entry_type for entry in entries] == [WalletEntryType.debit]
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_confirm_order_local_finalization_failure_keeps_provisioning_without_refund(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _url = two_sessions
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.aclose = AsyncMock()
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    monkeypatch.setattr(
        provisioning,
        "_finalize_active",
        AsyncMock(side_effect=RuntimeError("local finalization failed")),
    )
    async with factory() as session:
        user_id, state, message, _delivery = await _run_confirm_wallet_checkout(
            session, monkeypatch, client
        )
        assert state.cleared == 1
        await _assert_told_paid_and_pending(session, user_id, message)
        client.aclose.assert_awaited_once()

    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.user_id == user_id))
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order.id)
                )
            ).scalars()
        )
        assert order is not None and order.status is OrderStatus.PROVISIONING
        assert [entry.entry_type for entry in entries] == [WalletEntryType.debit]
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_confirm_order_success_delivers_without_refund(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _url = two_sessions
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.aclose = AsyncMock()
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    async with factory() as session:
        user_id, state, message, delivery = await _run_confirm_wallet_checkout(
            session, monkeypatch, client
        )
        assert state.cleared == 1
        delivery.assert_awaited_once()
        assert not message.answer.await_args_list
        client.aclose.assert_awaited_once()

    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.user_id == user_id))
        entries = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(WalletLedgerEntry.order_id == order.id)
                )
            ).scalars()
        )
        assert order is not None and order.status is OrderStatus.ACTIVE
        assert [entry.entry_type for entry in entries] == [WalletEntryType.debit]
        assert await get_balance(session, user_id) == 0


@pytest.mark.asyncio
async def test_apply_then_timeout_remains_provisioning_and_reuses_username_after_rollback(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    """An unknown create outcome must not refund or create another panel username."""
    factory, _url = two_sessions
    _user_id, _plan_id, order_id = await _seed_wallet_checkout(factory)
    panel_users: dict[str, UserResponse] = {}

    async def get_user(username: str) -> UserResponse:
        if username in panel_users:
            return panel_users[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    async def accept_then_timeout(payload: object) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _panel_user(username)
        raise TimeoutError("panel accepted then reply was lost")

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=accept_then_timeout)
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    async with factory() as session:
        with pytest.raises(TimeoutError, match="reply was lost"):
            await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        await session.rollback()

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        username = order.panel_username
        assert order.status is OrderStatus.PROVISIONING
        assert username in panel_users
        assert await get_balance(session, order.user_id) == 0
        client.create_user = AsyncMock(side_effect=AssertionError("must reconcile, not create"))
        await provision_order(session, client, order_id=order_id, default_group_ids=[1])
        await session.commit()
        assert order.status is OrderStatus.ACTIVE
        assert order.panel_username == username
        client.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_confirm_after_a_panel_500_resumes_the_paid_order(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer is told to try again; the retry must finish that order, not debit again."""
    factory, _url = two_sessions
    failing = AsyncMock()
    failing.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    failing.create_user = AsyncMock(side_effect=PasarGuardAPIError("server error", status_code=500))
    failing.aclose = AsyncMock()
    async with factory() as session:
        user_id, _state, _message, _delivery = await _run_confirm_wallet_checkout(
            session, monkeypatch, failing
        )

    healthy = AsyncMock()
    healthy.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    healthy.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    healthy.aclose = AsyncMock()
    healthy.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    delivery = AsyncMock()
    monkeypatch.setattr(purchase, "get_pasarguard_client", AsyncMock(return_value=healthy))
    monkeypatch.setattr(purchase, "deliver_provisioned_service_card", delivery)
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    settings = SimpleNamespace(max_discount_percent=100, wallet_provision_fail_credit_max_n=1)
    async with factory() as session:
        user = await session.get(User, user_id)
        await purchase.confirm_order(callback, _State(), session, settings, user)

    async with factory() as session:
        orders = list((await session.execute(select(Order))).scalars())
        debits = list(
            (
                await session.execute(
                    select(WalletLedgerEntry).where(
                        WalletLedgerEntry.entry_type == WalletEntryType.debit
                    )
                )
            ).scalars()
        )
        assert [o.status for o in orders] == [OrderStatus.ACTIVE]
        assert len(debits) == 1
        assert await get_balance(session, user_id) == 0
        delivery.assert_awaited_once()
        assert orders[0].panel_username == healthy.create_user.await_args.args[0].username
