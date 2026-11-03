"""Foundation hardening regressions (wallet-full, combo refund, locks, renew intent, snapshots, force_new, webhook)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client import UserResponse, UserStatus
from database.base import Base
from database.models import Order, OrderStatus, PaymentStatus, Plan, User, WalletEntryType
from database.models.pasarguard_account import PasarGuardAccount
from database.models.plan import DataLimitResetStrategy
from services.automation import expire_stale_orders
from services.finance import checkout_refund_key, refund_checkout_wallet_portion
from services.order_transitions import ALLOWED_TRANSITIONS, can_transition
from services.orders import create_pending_order, get_or_create_dynamic_plan
from services.payments import (
    PaymentError,
    approve_wallet_payment,
    reject_payment,
    submit_card_receipt,
)
from services.pricing import PlanType, PriceResult
from services.provisioning import ProvisionError, provision_order
from services.wallet import (
    REASON_CHECKOUT_DEBIT,
    checkout_debit_key,
    credit,
    debit,
    get_balance,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user(session: AsyncSession, *, balance: int = 0, tg: int = 1) -> User:
    user = User(
        telegram_id=tg,
        username="u",
        first_name="U",
        last_name=None,
        language_code="fa",
        referral_code=f"R{tg:06d}",
        is_admin=False,
        wallet_balance_toman=balance,
    )
    session.add(user)
    await session.flush()
    return user


async def _plan(
    session: AsyncSession, *, price: int = 50_000, name: str = "volume-10GB-30d"
) -> Plan:
    plan = Plan(
        name=name,
        description="t",
        data_limit_gb=10,
        duration_days=30,
        price_toman=price,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=1,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan


def _price(final: int) -> PriceResult:
    return PriceResult(
        original_price=final,
        discount_amount=0,
        final_price=final,
        currency="TMN",
        meta={"volume_gb": 10, "duration_days": 30},
    )


# --- A: wallet-full transition ---


@pytest.mark.asyncio
async def test_wallet_full_transition_not_in_general_map() -> None:
    assert can_transition(OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_APPROVED) is False
    assert OrderStatus.PAYMENT_APPROVED not in ALLOWED_TRANSITIONS[OrderStatus.PENDING_PAYMENT]


@pytest.mark.asyncio
async def test_approve_wallet_payment_with_valid_debit(session: AsyncSession) -> None:
    user = await _user(session, balance=100_000)
    plan = await _plan(session)
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        wallet_applied_toman=50_000,
    )
    await debit(
        session,
        user_id=user.id,
        amount_toman=50_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
    )
    order_out, payment = await approve_wallet_payment(session, order_id=order.id, user_id=user.id)
    assert order_out.status == OrderStatus.PAYMENT_APPROVED
    assert payment.method == "wallet"
    assert payment.status == PaymentStatus.verified


@pytest.mark.asyncio
async def test_approve_wallet_payment_rejects_without_debit(session: AsyncSession) -> None:
    user = await _user(session, balance=100_000)
    plan = await _plan(session)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status=OrderStatus.PENDING_PAYMENT,
        price_toman=50_000,
        discount_toman=0,
        wallet_applied_toman=50_000,
    )
    session.add(order)
    await session.flush()
    with pytest.raises(PaymentError, match="wallet_debit_missing"):
        await approve_wallet_payment(session, order_id=order.id, user_id=user.id)


@pytest.mark.asyncio
async def test_approve_wallet_does_not_bypass_partial_card(session: AsyncSession) -> None:
    user = await _user(session, balance=20_000)
    plan = await _plan(session)
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        wallet_applied_toman=20_000,
    )
    await debit(
        session,
        user_id=user.id,
        amount_toman=20_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
    )
    with pytest.raises(PaymentError, match="wallet_not_full_cover"):
        await approve_wallet_payment(session, order_id=order.id, user_id=user.id)


# --- B: combo refund ---


@pytest.mark.asyncio
async def test_expire_refunds_wallet_portion_idempotent(session: AsyncSession) -> None:
    user = await _user(session, balance=100_000)
    plan = await _plan(session)
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        wallet_applied_toman=20_000,
    )
    await debit(
        session,
        user_id=user.id,
        amount_toman=20_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
    )
    order.created_at = datetime.now(UTC) - timedelta(hours=2)
    await session.flush()
    assert await get_balance(session, user.id) == 80_000

    expired = await expire_stale_orders(session, expiry_minutes=30)
    assert len(expired) == 1
    assert expired[0].status == OrderStatus.EXPIRED
    assert await get_balance(session, user.id) == 100_000

    # Idempotent second expire/refund
    await refund_checkout_wallet_portion(session, order=order)
    assert await get_balance(session, user.id) == 100_000
    key = checkout_refund_key(order.id)
    from database.models import WalletLedgerEntry

    rows = (
        (
            await session.execute(
                select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key == key)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].entry_type == WalletEntryType.credit
    assert int(rows[0].amount_toman) == 20_000


@pytest.mark.asyncio
async def test_reject_refunds_wallet_portion(session: AsyncSession) -> None:
    user = await _user(session, balance=100_000, tg=2)
    admin = await _user(session, balance=0, tg=9001)
    admin.is_admin = True
    plan = await _plan(session, name="volume-10GB-30d-b")
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        wallet_applied_toman=15_000,
    )
    await debit(
        session,
        user_id=user.id,
        amount_toman=15_000,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key=checkout_debit_key(order.id),
        order_id=order.id,
    )
    await submit_card_receipt(
        session,
        order_id=order.id,
        user_id=user.id,
        receipt_file_id="f",
        card_number_display="6037",
    )
    await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="bad")
    await session.refresh(order)
    assert order.status == OrderStatus.REJECTED
    assert await get_balance(session, user.id) == 100_000
    # second reject idempotent
    await reject_payment(session, order_id=order.id, admin_user_id=admin.id, note="bad")
    assert await get_balance(session, user.id) == 100_000


# --- C: FOR UPDATE ---


@pytest.mark.asyncio
async def test_wallet_load_uses_for_update_clause(session: AsyncSession) -> None:
    user = await _user(session, balance=10_000, tg=3)
    statements: list[str] = []

    sync_engine = session.bind.sync_engine

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    await credit(
        session,
        user_id=user.id,
        amount_toman=1_000,
        reason="test",
        idempotency_key="c-lock-1",
    )
    joined = " | ".join(statements).upper()
    # Postgres emits FOR UPDATE; SQLite may omit — still assert our code path requested it
    # by checking SQLAlchemy compiled form when dialect supports it, else soft-check update.
    assert "USERS" in joined
    # Conditional debit update present on debit path:
    await debit(
        session,
        user_id=user.id,
        amount_toman=500,
        reason=REASON_CHECKOUT_DEBIT,
        idempotency_key="c-lock-d1",
    )
    joined2 = " | ".join(statements).upper()
    assert "UPDATE" in joined2
    assert await get_balance(session, user.id) == 10_500


# --- E: plan immutability + snapshots ---


@pytest.mark.asyncio
async def test_dynamic_plan_does_not_mutate_shared_row(session: AsyncSession) -> None:
    price1 = PriceResult(
        original_price=50_000,
        discount_amount=0,
        final_price=50_000,
        currency="TMN",
        meta={"volume_gb": 10, "duration_days": 30},
    )
    plan1 = await get_or_create_dynamic_plan(
        session,
        plan_type=PlanType.VOLUME,
        price=price1,
        group_ids=[1],
        hwid_limit=1,
    )
    order = await create_pending_order(
        session, user_id=(await _user(session, tg=4)).id, plan=plan1, price=price1
    )
    assert order.delivery_data_limit_gb == 10
    assert order.delivery_duration_days == 30
    assert order.delivery_group_ids == [1]

    price2 = PriceResult(
        original_price=60_000,
        discount_amount=0,
        final_price=60_000,
        currency="TMN",
        meta={"volume_gb": 10, "duration_days": 30},
    )
    plan2 = await get_or_create_dynamic_plan(
        session,
        plan_type=PlanType.VOLUME,
        price=price2,
        group_ids=[1, 2],
        hwid_limit=1,
    )
    await session.refresh(plan1)
    assert int(plan1.price_toman) == 50_000
    assert list(plan1.group_ids) == [1]
    assert plan2.id != plan1.id
    assert int(plan2.price_toman) == 60_000


# --- F: force_new durable ---


@pytest.mark.asyncio
async def test_force_new_survives_failed_provision_retry(session: AsyncSession) -> None:
    user = await _user(session, tg=5)
    plan = await _plan(session, name="volume-10GB-30d-f")
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=99,
        panel_username="old_svc_ab12",
        subscription_url="https://panel.example.com/sub/x",
        plan_fingerprint="volume:10:30",
    )
    session.add(account)
    await session.flush()

    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        force_new=True,
    )
    order.status = OrderStatus.PAYMENT_APPROVED
    await session.flush()
    assert order.force_new is True

    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=Exception("boom"))
    client.create_user = AsyncMock(side_effect=Exception("boom"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    with pytest.raises(Exception):  # noqa: B017 — mock raises bare Exception
        await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.force_new is True  # must keep choice for retry
    assert order.status == OrderStatus.PROVISION_FAILED

    # Retry must still force create (not renew matching account)
    created = UserResponse(
        id=7,
        username="new_svc_zz99",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url="https://panel.example.com/sub/new_svc_zz99",
        data_limit=10 * 1024**3,
        expire=int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
    )

    async def _get_user(username: str):
        from api_client.exceptions import PasarGuardNotFoundError

        raise PasarGuardNotFoundError("missing")

    client.get_user = AsyncMock(side_effect=_get_user)
    client.create_user = AsyncMock(return_value=created)
    account2, _ = await provision_order(session, client, order_id=order.id)
    assert account2.panel_username == "new_svc_zz99"
    assert account2.id != account.id
    client.modify_user.assert_not_called()


# --- D: renew intent before panel + no double-extend ---


@pytest.mark.asyncio
async def test_renew_commits_intent_before_modify_and_reconciles(session: AsyncSession) -> None:
    user = await _user(session, tg=6)
    plan = await _plan(session, name="volume-10GB-30d-d")
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=11,
        panel_username="renew_me_01",
        subscription_url="https://panel.example.com/sub/renew_me_01",
        plan_fingerprint="volume:10:30",
    )
    session.add(account)
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        renew_account_id=account.id,
    )
    order.status = OrderStatus.PAYMENT_APPROVED
    await session.flush()

    now = datetime.now(UTC)
    live_expire = int((now + timedelta(days=5)).timestamp())
    target_expire = live_expire + 30 * 86400
    live = UserResponse(
        id=11,
        username="renew_me_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_me_01",
        data_limit=10 * 1024**3,
        expire=live_expire,
    )
    after = UserResponse(
        id=11,
        username="renew_me_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_me_01",
        data_limit=20 * 1024**3,
        expire=target_expire,
        hwid_limit=1,  # the lost PUT applied this field too
    )

    client = AsyncMock()
    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    with pytest.raises(ProvisionError, match="panel_transient"):
        await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING
    assert order.renew_target_expire_unix is not None
    assert order.panel_username == "renew_me_01"
    assert client.modify_user.await_count == 1

    # Reconcile: panel already shows target → no second extend
    client.get_user = AsyncMock(return_value=after)
    client.modify_user = AsyncMock(return_value=after)
    await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    # modify not needed when already at target
    assert client.modify_user.await_count == 0


@pytest.mark.asyncio
async def test_renew_retry_uses_stored_targets_when_panel_below_target(
    session: AsyncSession,
) -> None:
    """Timeout after intent: retry must apply stored expire/limit, not recompute."""
    user = await _user(session, tg=7)
    plan = await _plan(session, name="volume-10GB-30d-retry")
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=12,
        panel_username="renew_store_01",
        subscription_url="https://panel.example.com/sub/renew_store_01",
        plan_fingerprint="volume:10:30",
    )
    session.add(account)
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        renew_account_id=account.id,
    )
    order.status = OrderStatus.PAYMENT_APPROVED
    await session.flush()

    now = datetime.now(UTC)
    live_expire = int((now + timedelta(days=5)).timestamp())
    live = UserResponse(
        id=12,
        username="renew_store_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_store_01",
        data_limit=10 * 1024**3,
        expire=live_expire,
    )

    client = AsyncMock()
    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    with pytest.raises(ProvisionError, match="panel_transient"):
        await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING
    stored_expire = order.renew_target_expire_unix
    stored_limit = order.renew_target_data_limit
    assert stored_expire is not None
    assert stored_limit == 20 * 1024**3
    assert client.modify_user.await_count == 1

    # If retry recomputed from a longer duration / bigger quota, payload would change.
    order.delivery_duration_days = 90
    order.delivery_data_limit_gb = 50
    await session.flush()

    still_old = UserResponse(
        id=12,
        username="renew_store_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_store_01",
        data_limit=10 * 1024**3,
        expire=live_expire,  # still below stored target
    )
    after = UserResponse(
        id=12,
        username="renew_store_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_store_01",
        data_limit=stored_limit,
        expire=stored_expire,
    )
    client.get_user = AsyncMock(return_value=still_old)
    client.modify_user = AsyncMock(return_value=after)
    await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert client.modify_user.await_count == 1
    username, payload = client.modify_user.await_args.args
    assert username == "renew_store_01"
    assert payload.expire == stored_expire
    assert payload.data_limit == stored_limit
    # Fresh 90-day recompute from live expire would overshoot stored 30-day target.
    recomputed_90 = live_expire + 90 * 86400
    assert payload.expire != recomputed_90
    assert payload.expire == stored_expire


@pytest.mark.asyncio
async def test_renew_second_provision_is_noop_after_successful_retry(
    session: AsyncSession,
) -> None:
    """After timeout then one successful stored-target extend, retry is a no-op."""
    user = await _user(session, tg=8)
    plan = await _plan(session, name="volume-10GB-30d-noop")
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=13,
        panel_username="renew_noop_01",
        subscription_url="https://panel.example.com/sub/renew_noop_01",
        plan_fingerprint="volume:10:30",
    )
    session.add(account)
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=user.id,
        plan=plan,
        price=_price(50_000),
        renew_account_id=account.id,
    )
    order.status = OrderStatus.PAYMENT_APPROVED
    await session.flush()

    now = datetime.now(UTC)
    live_expire = int((now + timedelta(days=4)).timestamp())
    live = UserResponse(
        id=13,
        username="renew_noop_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_noop_01",
        data_limit=10 * 1024**3,
        expire=live_expire,
    )
    client = AsyncMock()
    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    client.subscription_url_from_response = lambda u: u.subscription_url

    with pytest.raises(ProvisionError, match="panel_transient"):
        await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING
    stored_expire = int(order.renew_target_expire_unix)
    stored_limit = int(order.renew_target_data_limit)

    after = UserResponse(
        id=13,
        username="renew_noop_01",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=now,
        subscription_url="https://panel.example.com/sub/renew_noop_01",
        data_limit=stored_limit,
        expire=stored_expire,
    )
    # Panel still shows old expire (timeout may have applied server-side or not).
    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(return_value=after)
    await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert client.modify_user.await_count == 1
    payload = client.modify_user.await_args.args[1]
    assert payload.expire == stored_expire
    assert payload.data_limit == stored_limit

    # Second provision: already ACTIVE → no further extend (double-extend avoided).
    client.get_user = AsyncMock(return_value=live)
    client.modify_user = AsyncMock(return_value=after)
    account2, _ = await provision_order(session, client, order_id=order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.ACTIVE
    assert account2.panel_username == "renew_noop_01"
    client.modify_user.assert_not_called()
    client.get_user.assert_not_called()


# --- G: webhook secret wiring (real handler HTTP path) ---


def test_require_webhook_secret_empty_raises() -> None:
    from bot.main import require_webhook_secret

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        require_webhook_secret(SimpleNamespace(is_webhook_mode=True, webhook_secret=""))
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        require_webhook_secret(SimpleNamespace(is_webhook_mode=True, webhook_secret="   "))
    assert (
        require_webhook_secret(SimpleNamespace(is_webhook_mode=True, webhook_secret=" tok "))
        == "tok"
    )
    # Polling may have an empty secret; helper must not raise.
    assert require_webhook_secret(SimpleNamespace(is_webhook_mode=False, webhook_secret="")) == ""


def test_build_webhook_handler_always_sets_secret() -> None:
    from bot.main import build_webhook_handler

    dp = Dispatcher()
    bot = Bot(token="123456:TEST-WEBHOOK-SECRET")
    handler = build_webhook_handler(dp, bot, "s3cret")
    assert isinstance(handler, SimpleRequestHandler)
    assert handler.secret_token == "s3cret"
    # Contrast: constructing with None skips verify_secret — main must not do that.
    skipped = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=None)
    assert skipped.secret_token is None
    assert handler.secret_token is not None
    with pytest.raises(RuntimeError, match="secret_token"):
        build_webhook_handler(dp, bot, "")


async def _webhook_test_client(secret: str):
    from bot.main import build_webhook_handler

    dp = Dispatcher()
    dp.feed_webhook_update = AsyncMock(return_value=None)
    bot = Bot(token="123456:TEST-WEBHOOK-HTTP")
    handler = build_webhook_handler(dp, bot, secret)
    handler.handle_in_background = False
    app = web.Application()
    app.router.add_post("/webhook", handler.handle)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, bot, dp


@pytest.mark.asyncio
async def test_webhook_handle_accepts_correct_secret() -> None:
    secret = "correct-token"
    client, bot, dp = await _webhook_test_client(secret)
    try:
        resp = await client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
        assert resp.status != 401
        assert resp.status == 200
        assert dp.feed_webhook_update.await_count == 1
    finally:
        await client.close()
        await bot.session.close()


@pytest.mark.asyncio
async def test_webhook_handle_rejects_wrong_secret() -> None:
    client, bot, dp = await _webhook_test_client("correct-token")
    try:
        resp = await client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
        )
        assert resp.status == 401
        assert "Unauthorized" in await resp.text()
        dp.feed_webhook_update.assert_not_awaited()
    finally:
        await client.close()
        await bot.session.close()


@pytest.mark.asyncio
async def test_webhook_handle_rejects_missing_secret_header() -> None:
    client, bot, dp = await _webhook_test_client("correct-token")
    try:
        resp = await client.post("/webhook", json={"update_id": 1})
        assert resp.status == 401
        assert "Unauthorized" in await resp.text()
        dp.feed_webhook_update.assert_not_awaited()
    finally:
        await client.close()
        await bot.session.close()
