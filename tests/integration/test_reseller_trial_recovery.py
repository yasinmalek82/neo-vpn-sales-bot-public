"""Persistent recovery and settlement for independent reseller giveaways."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_client import UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError, PasarGuardAuthError, PasarGuardNotFoundError
from bot.handlers import referral
from bot.keyboards.referral import reseller_trial_recovery_keyboard
from database.models import Order, OrderStatus, PasarGuardAccount, Plan, User, WalletEntryType
from database.models.order import TRIAL_KIND_PERSONAL
from database.models.plan import DataLimitResetStrategy
from database.models.wallet import WalletLedgerEntry
from services.finance import settle_reseller_trial_provision_failure
from services.reseller import set_rate
from services.trial import (
    TrialError,
    TrialResult,
    get_reseller_trial_quota,
    issue_reseller_trial,
    resume_reseller_trial,
)
from services.wallet import credit, retry_debit_key

PRICE = 5_000


async def _seed_shop(factory: async_sessionmaker[AsyncSession], *, balance: int = 0) -> int:
    async with factory() as session:
        shop = User(telegram_id=830, referral_code="RS830", is_reseller=True)
        session.add(shop)
        await session.flush()
        await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=PRICE)
        if balance:
            await credit(
                session,
                user_id=shop.id,
                amount_toman=balance,
                reason="topup",
                idempotency_key="reseller-recovery-topup",
            )
        await session.commit()
        return int(shop.id)


def _user(username: str, *, panel_id: int = 900) -> UserResponse:
    return UserResponse(
        id=panel_id,
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example/sub/{username}",
        data_limit=2 * 1024**3,
        expire=1_800_000_000,
    )


def _client(panel_users: dict[str, UserResponse], create: Any) -> AsyncMock:
    async def get_user(username: str) -> UserResponse:
        if username in panel_users:
            return panel_users[username]
        raise PasarGuardNotFoundError("missing", status_code=404)

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=get_user)
    client.create_user = AsyncMock(side_effect=create)
    client.subscription_url_from_response = lambda panel_user: panel_user.subscription_url
    return client


async def _issue(
    session: AsyncSession, client: AsyncMock, shop_id: int, *, label: str = "رضا | 0912"
):
    return await issue_reseller_trial(
        session,
        client,
        user_id=shop_id,
        data_limit_gb=2,
        duration_days=1,
        hwid_limit=1,
        group_ids=[7],
        display_name=label,
    )


@pytest.mark.asyncio
async def test_lost_reply_explicit_resume_keeps_one_paid_order_debit_and_account(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    panel_users: dict[str, UserResponse] = {}

    async def accept_then_lose(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _user(username)
        raise TimeoutError("reply lost")

    client = _client(panel_users, accept_then_lose)
    async with factory() as first:
        with pytest.raises(TrialError, match="reply lost") as raised:
            await _issue(first, client, shop_id)
        assert raised.value.order_id is not None
        order_id = raised.value.order_id
        await first.commit()

    async with factory() as resumed:
        result = await resume_reseller_trial(resumed, client, user_id=shop_id, order_id=order_id)
        await resumed.commit()
        assert result.order.id == order_id
        assert (result.delivery_data_limit_gb, result.delivery_duration_days) == (2, 1)
        assert result.account.panel_username in panel_users
        assert await resumed.scalar(select(func.count()).select_from(Order)) == 1
        entries = list((await resumed.execute(select(WalletLedgerEntry))).scalars())
        assert [(entry.entry_type, entry.amount_toman, entry.reason) for entry in entries] == [
            (WalletEntryType.credit, 10_000, "topup"),
            (WalletEntryType.debit, PRICE, "reseller_trial"),
        ]
    assert client.create_user.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_exception",
    [
        PasarGuardAPIError("bad request", status_code=400),
        PasarGuardAPIError("bad group", status_code=422),
        PasarGuardAuthError("expired key", status_code=401),
    ],
)
async def test_terminal_rejection_refunds_exact_paid_trial_then_resume_debits_same_order(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    terminal_exception: Exception,
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    panel_users: dict[str, UserResponse] = {}

    async def reject(_payload: Any) -> UserResponse:
        raise terminal_exception

    client = _client(panel_users, reject)
    async with factory() as first:
        with pytest.raises(TrialError) as raised:
            await _issue(first, client, shop_id, label="سارا | 0935")
        order_id = raised.value.order_id
        assert order_id is not None
        assert (
            await settle_reseller_trial_provision_failure(
                first,
                order_id=order_id,
                original_exception=raised.value.original_exception,
                max_n=1,
            )
            == PRICE
        )
        await first.commit()

    async with factory() as check:
        order = await check.get(Order, order_id)
        shop = await check.get(User, shop_id)
        assert order is not None and order.status is OrderStatus.PROVISION_FAILED
        assert shop is not None and shop.wallet_balance_toman == 10_000

    async def accept(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _user(username)
        return panel_users[username]

    client.create_user = AsyncMock(side_effect=accept)
    async with factory() as resumed:
        result = await resume_reseller_trial(resumed, client, user_id=shop_id, order_id=order_id)
        await resumed.commit()
        assert result.order.id == order_id
        debits = list(
            (
                await resumed.execute(
                    select(WalletLedgerEntry).where(
                        WalletLedgerEntry.idempotency_key == retry_debit_key(order_id)
                    )
                )
            ).scalars()
        )
        assert len(debits) == 1
        assert debits[0].entry_type is WalletEntryType.debit
        assert debits[0].reason == "retry_debit"
        assert debits[0].amount_toman == PRICE


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "wrong_reason"])
async def test_missing_or_malformed_original_reseller_debit_never_refunds(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    corruption: str,
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    panel_users: dict[str, UserResponse] = {}

    async def reject(_payload: Any) -> UserResponse:
        raise PasarGuardAPIError("bad group", status_code=422)

    client = _client(panel_users, reject)
    async with factory() as session:
        with pytest.raises(TrialError) as raised:
            await _issue(session, client, shop_id)
        order_id = raised.value.order_id
        assert order_id is not None
        entry = await session.scalar(
            select(WalletLedgerEntry).where(
                WalletLedgerEntry.idempotency_key == f"reseller_trial:{order_id}"
            )
        )
        assert entry is not None
        if corruption == "missing":
            await session.delete(entry)
        else:
            entry.reason = "wrong_reason"
        assert (
            await settle_reseller_trial_provision_failure(
                session,
                order_id=order_id,
                original_exception=raised.value.original_exception,
                max_n=1,
            )
            == 0
        )
        await session.commit()
    async with factory() as check:
        shop = await check.get(User, shop_id)
        assert shop is not None and shop.wallet_balance_toman == 5_000

    no_io = _client({}, AssertionError("unverified payment must not reach panel"))
    async with factory() as check:
        with pytest.raises(TrialError, match="reseller_trial_payment_unverified"):
            await resume_reseller_trial(check, no_io, user_id=shop_id, order_id=order_id)
        assert (
            await check.scalar(
                select(func.count())
                .select_from(WalletLedgerEntry)
                .where(WalletLedgerEntry.idempotency_key == retry_debit_key(order_id))
            )
            == 0
        )
    no_io.get_user.assert_not_awaited()
    no_io.create_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unconfirmed_exception",
    [
        TimeoutError("reply lost"),
        PasarGuardAuthError("forbidden", status_code=403),
        PasarGuardAPIError("not found", status_code=404),
        PasarGuardAPIError("conflict", status_code=409),
        PasarGuardAPIError("panel down", status_code=500),
    ],
)
async def test_unconfirmed_reseller_create_keeps_provisioning_and_never_refunds(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
    unconfirmed_exception: Exception,
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    client = _client({}, unconfirmed_exception)
    async with factory() as session:
        with pytest.raises(TrialError) as raised:
            await _issue(session, client, shop_id)
        order_id = raised.value.order_id
        assert order_id is not None
        assert (
            await settle_reseller_trial_provision_failure(
                session,
                order_id=order_id,
                original_exception=raised.value.original_exception,
                max_n=1,
            )
            == 0
        )
        await session.commit()
    async with factory() as check:
        order = await check.get(Order, order_id)
        assert order is not None and order.status is OrderStatus.PROVISIONING
        assert (
            await check.scalar(
                select(func.count())
                .select_from(WalletLedgerEntry)
                .where(WalletLedgerEntry.entry_type == WalletEntryType.credit)
            )
            == 1
        )  # topup only


@pytest.mark.asyncio
async def test_free_reseller_resume_reuses_same_order_without_retry_debit(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    async with factory() as seed:
        shop = User(telegram_id=831, referral_code="RS831", is_reseller=True)
        seed.add(shop)
        await seed.flush()
        await set_rate(seed, shop.id, trial_quota_monthly=1, trial_price_toman=PRICE)
        shop_id = int(shop.id)
        await seed.commit()
    panel_users: dict[str, UserResponse] = {}
    client = _client(panel_users, TimeoutError("reply lost"))
    async with factory() as first:
        with pytest.raises(TrialError) as raised:
            await _issue(first, client, shop_id)
        order_id = raised.value.order_id
        assert order_id is not None
        await first.commit()

    async def accept(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _user(username)
        return panel_users[username]

    client.create_user = AsyncMock(side_effect=accept)
    async with factory() as resumed:
        result = await resume_reseller_trial(resumed, client, user_id=shop_id, order_id=order_id)
        await resumed.commit()
        assert result.order.id == order_id
        assert await resumed.scalar(select(func.count()).select_from(Order)) == 1
        assert await resumed.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0


@pytest.mark.asyncio
async def test_new_customer_always_creates_new_reseller_operation(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=20_000)
    panel_users: dict[str, UserResponse] = {}

    async def accept(payload: Any) -> UserResponse:
        username = str(payload.username)
        panel_users[username] = _user(username, panel_id=len(panel_users) + 900)
        return panel_users[username]

    client = _client(panel_users, accept)
    async with factory() as session:
        first = await _issue(session, client, shop_id, label="مشتری اول")
        second = await _issue(session, client, shop_id, label="مشتری دوم")
        assert first.order.id != second.order.id
        assert first.order.customer_label == "مشتری اول"
        assert second.order.customer_label == "مشتری دوم"
        assert first.order.force_new is True and second.order.force_new is True


@pytest.mark.asyncio
async def test_reseller_quota_excludes_explicit_personal_trial_but_counts_legacy_unknown(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory)
    async with factory() as session:
        await set_rate(session, shop_id, trial_quota_monthly=5, trial_price_toman=PRICE)
        plan = Plan(
            name="trial-quota-recovery",
            description="test",
            data_limit_gb=2,
            duration_days=1,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=shop_id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                discount_toman=0,
                trial_kind=TRIAL_KIND_PERSONAL,
            )
        )
        await session.flush()
        assert (await get_reseller_trial_quota(session, shop_id)).used == 0
        session.add(
            Order(
                user_id=shop_id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=0,
                discount_toman=0,
                trial_kind=None,
            )
        )
        await session.flush()
        assert (await get_reseller_trial_quota(session, shop_id)).used == 1


@pytest.mark.asyncio
async def test_resume_rejects_wrong_owner_kind_and_active_fallback_without_panel_io(
    two_sessions: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _url = two_sessions
    shop_id = await _seed_shop(factory, balance=10_000)
    async with factory() as session:
        other = User(telegram_id=832, referral_code="RS832", is_reseller=True)
        session.add(other)
        await session.flush()
        other_id = int(other.id)

        # Reuse a genuine issued plan/order so FK and snapshots reflect the path.
        async def accept(payload: Any) -> UserResponse:
            return _user(str(payload.username))

        client = _client({}, accept)
        issued = await _issue(session, client, shop_id)
        issued.order.status = OrderStatus.ACTIVE
        issued.order.panel_username = "exact-only"
        session.add(PasarGuardAccount(user_id=shop_id, panel_user_id=991, panel_username="other"))
        await session.commit()
        order_id = int(issued.order.id)

    no_io = _client({}, AssertionError("ACTIVE recovery must not touch panel"))
    async with factory() as session:
        with pytest.raises(TrialError, match="trial_recovery_unsafe"):
            await resume_reseller_trial(session, no_io, user_id=shop_id, order_id=order_id)
        with pytest.raises(TrialError, match="reseller_trial_not_found"):
            await resume_reseller_trial(session, no_io, user_id=other_id, order_id=order_id)
        order = await session.get(Order, order_id)
        assert order is not None
        order.trial_kind = TRIAL_KIND_PERSONAL
        with pytest.raises(TrialError, match="reseller_trial_not_found"):
            await resume_reseller_trial(session, no_io, user_id=shop_id, order_id=order_id)
    no_io.get_user.assert_not_awaited()
    no_io.create_user.assert_not_awaited()


def test_reseller_retry_button_names_the_original_customer() -> None:
    keyboard = reseller_trial_recovery_keyboard(order_id=44, customer_label="نسرین | 0912")
    button = keyboard.inline_keyboard[0][0]
    assert "نسرین | 0912" in button.text
    assert button.callback_data == "ref:reseller-retry:44"


def test_reseller_resume_callback_parser_is_strict() -> None:
    assert referral._reseller_resume_order_id("ref:reseller-retry:44") == 44
    assert referral._reseller_resume_order_id("ref:reseller-retry:0") is None
    assert referral._reseller_resume_order_id("ref:reseller-retry:44:other") is None


def test_unsafe_or_unverified_resume_does_not_offer_another_retry() -> None:
    order = SimpleNamespace(id=44, customer_label="نسرین")
    for code in (
        "trial_recovery_unsafe",
        "reseller_trial_payment_unverified",
        "reseller_trial_not_found",
    ):
        markup = referral._reseller_recovery_markup(TrialError(code, order_id=44), order)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "ref:reseller-retry:44" not in callbacks
    recoverable = referral._reseller_recovery_markup(
        TrialError("panel down", order_id=44, original_exception=RuntimeError("down")), order
    )
    assert recoverable.inline_keyboard[0][0].callback_data == "ref:reseller-retry:44"


@pytest.mark.asyncio
async def test_reseller_delivery_copy_uses_order_snapshot_after_settings_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = SimpleNamespace(
        id=44, price_toman=0, delivery_data_limit_gb=2, delivery_duration_days=1
    )
    account = SimpleNamespace(id=81, subscription_url="https://panel.example/sub/old")
    result = TrialResult(order=order, account=account, already_claimed=False)  # type: ignore[arg-type]
    message = AsyncMock()
    deliver = AsyncMock()
    monkeypatch.setattr(
        referral, "get_reseller_trial_quota", AsyncMock(return_value=SimpleNamespace(remaining=3))
    )
    monkeypatch.setattr(referral, "deliver_provisioned_service_card", deliver)
    await referral._deliver_reseller_trial_result(
        message,
        AsyncMock(),
        SimpleNamespace(default_hwid_limit_volume=99),
        SimpleNamespace(id=1),
        result,
    )
    assert "۲" in deliver.await_args.kwargs["intro"]
    assert "۱" in deliver.await_args.kwargs["intro"]
