"""G1: a receipt for an order that already expired reaches the owner, never revives it."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from bot.handlers import admin as admin_handlers
from bot.handlers import payment as payment_handlers
from bot.keyboards.admin import AdminCB
from bot.states import PurchaseStates
from bot.texts import texts
from database.base import Base
from database.models import Order, OrderStatus, Payment, Plan, User, WalletLedgerEntry
from database.models.plan import DataLimitResetStrategy
from database.models.report import TOPIC_PAYMENTS, ReportOutbox
from services.orders import create_pending_order
from services.pricing import PriceResult
from services.wallet import get_balance

ADMIN_ID = 4242
OTHER_ADMIN_ID = 4343
PRICE = 50_000
WALLET_PART = 10_000
CARD_DUE = PRICE - WALLET_PART


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        admin_telegram_ids=[ADMIN_ID, OTHER_ADMIN_ID],
        payment_card_number="6037991234567890",
        payment_card_holder_name="x",
        payment_bank_name="y",
        payment_instructions="",
    )


def _state(tg_id: int = 8001) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=tg_id, user_id=tg_id),
    )


def _photo_message() -> SimpleNamespace:
    return SimpleNamespace(
        photo=[SimpleNamespace(file_id="late-file")],
        document=None,
        answer=AsyncMock(),
    )


async def _expired_order(session: AsyncSession) -> tuple[User, Order]:
    buyer = User(telegram_id=8001, referral_code="BUY8001", first_name="Ali")
    plan = Plan(
        name="late",
        description="test",
        data_limit_gb=10,
        duration_days=30,
        price_toman=PRICE,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        group_ids=[1],
        hwid_limit=0,
        is_active=True,
    )
    session.add_all([buyer, plan])
    await session.flush()
    order = await create_pending_order(
        session,
        user_id=buyer.id,
        plan=plan,
        price=PriceResult(
            original_price=PRICE,
            discount_amount=0,
            final_price=PRICE,
            currency="IRT",
            meta={"volume_gb": 10, "duration_days": 30},
        ),
        service_name="ali",
    )
    # The wallet portion was refunded when the order expired; the card part is what
    # the customer can still have paid.
    order.wallet_applied_toman = WALLET_PART
    order.status = OrderStatus.EXPIRED
    await session.commit()
    return buyer, order


async def _submit(
    session: AsyncSession, buyer: User, order: Order, bot: SimpleNamespace
) -> SimpleNamespace:
    message = _photo_message()
    state = _state(buyer.telegram_id)
    await state.set_state(PurchaseStates.waiting_for_receipt)
    await state.update_data(pending_order_id=order.id)
    await payment_handlers.receive_receipt(
        message,  # type: ignore[arg-type]
        state,
        session,
        _settings(),  # type: ignore[arg-type]
        bot,  # type: ignore[arg-type]
        buyer,
    )
    return message


def _bot() -> SimpleNamespace:
    return SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())


def _buttons(markup: object) -> list[object]:
    return [b for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_receipt_for_an_expired_order_reaches_the_admins_and_the_customer(
    session: AsyncSession,
) -> None:
    buyer, order = await _expired_order(session)
    bot = _bot()

    message = await _submit(session, buyer, order, bot)

    # Every admin gets the receipt photo.
    sent_to = [call.kwargs["chat_id"] for call in bot.send_photo.await_args_list]
    assert sent_to == [ADMIN_ID, OTHER_ADMIN_ID]
    call = bot.send_photo.await_args_list[0]
    assert call.kwargs["photo"] == "late-file"
    assert f"#{order.id}" in call.kwargs["caption"]
    # The order is not revived and no payment row was made.
    await session.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert await session.scalar(select(func.count()).select_from(Payment)) == 0
    # The customer is told what happens, not handed the generic error.
    shown = message.answer.await_args_list[0].args[0]
    assert shown != texts.payment_submit_error
    assert shown.startswith(texts.notify_late_receipt.split("{")[0])
    # Recorded once in the outbox, payments topic.
    rows = (await session.execute(select(ReportOutbox))).scalars().all()
    assert [(r.event_key, r.topic_key) for r in rows] == [
        (f"late_receipt:{order.id}", TOPIC_PAYMENTS)
    ]


@pytest.mark.asyncio
async def test_a_second_late_receipt_does_not_notify_the_admins_again(
    session: AsyncSession,
) -> None:
    buyer, order = await _expired_order(session)
    bot = _bot()

    first = await _submit(session, buyer, order, bot)
    second = await _submit(session, buyer, order, bot)

    assert bot.send_photo.await_count == 2  # the two admins, once
    assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 1
    # Both submissions still get the customer notice.
    assert first.answer.await_count == 1
    assert second.answer.await_count == 1
    assert second.answer.await_args_list[0].args[0] != texts.payment_submit_error


@pytest.mark.asyncio
async def test_the_same_photo_sent_again_without_any_setup_gets_the_notice_again(
    session: AsyncSession,
) -> None:
    """The first late receipt must leave the customer where the next photo is still a receipt."""
    buyer, order = await _expired_order(session)
    bot = _bot()
    state = _state(buyer.telegram_id)
    await state.set_state(PurchaseStates.waiting_for_receipt)
    await state.update_data(pending_order_id=order.id)
    messages = []
    for _ in range(2):
        message = _photo_message()
        messages.append(message)
        await payment_handlers.receive_receipt(
            message,  # type: ignore[arg-type]
            state,
            session,
            _settings(),  # type: ignore[arg-type]
            bot,  # type: ignore[arg-type]
            buyer,
        )
    shown = [m.answer.await_args_list[0].args[0] for m in messages]
    assert shown[0] == shown[1]
    assert shown[1].startswith(texts.notify_late_receipt.split("{")[0])
    assert bot.send_photo.await_count == 2  # the two admins, from the first photo only


@pytest.mark.asyncio
async def test_a_late_receipt_for_someone_elses_order_is_refused(
    session: AsyncSession,
) -> None:
    _buyer, order = await _expired_order(session)
    stranger = User(telegram_id=9009, referral_code="STR9009")
    session.add(stranger)
    await session.commit()
    bot = _bot()

    message = await _submit(session, stranger, order, bot)

    bot.send_photo.assert_not_awaited()
    assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 0
    assert message.answer.await_args_list[0].args[0] == texts.payment_submit_error


@pytest.mark.asyncio
async def test_the_wallet_button_carries_the_card_due_and_credits_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    buyer, order = await _expired_order(session)
    buyer_id, order_id = buyer.id, order.id
    for tg in (ADMIN_ID, OTHER_ADMIN_ID):
        session.add(User(telegram_id=tg, referral_code=f"ADM{tg}", is_admin=True))
    await session.commit()
    bot = _bot()
    await _submit(session, buyer, order, bot)

    markup = bot.send_photo.await_args_list[0].kwargs["reply_markup"]
    buttons = _buttons(markup)
    credit_btn = next(b for b in buttons if b.callback_data.startswith(AdminCB.LATE_CREDIT))  # type: ignore[attr-defined]
    ignore_btn = next(b for b in buttons if b.callback_data.startswith(AdminCB.LATE_IGNORE))  # type: ignore[attr-defined]
    assert "شارژ کیف پول" in credit_btn.text  # type: ignore[attr-defined]
    assert "۴۰٬۰۰۰" in credit_btn.text  # type: ignore[attr-defined]
    assert ignore_btn.text.startswith("❌")  # type: ignore[attr-defined]
    assert len(credit_btn.callback_data.encode()) <= 64  # type: ignore[attr-defined]

    admin = await session.scalar(select(User).where(User.telegram_id == ADMIN_ID))
    admin_state = _state(ADMIN_ID)
    screen = SimpleNamespace(
        edit_text=AsyncMock(), answer=AsyncMock(), edit_reply_markup=AsyncMock()
    )
    monkeypatch.setattr(admin_handlers, "_tg_message", lambda _cb: screen)

    def cb(data: str, tg: int = ADMIN_ID) -> SimpleNamespace:
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=tg), answer=AsyncMock())

    await admin_handlers.admin_late_receipt_credit(
        cb(credit_btn.callback_data),  # type: ignore[attr-defined]
        admin_state,
        session,
        _settings(),  # type: ignore[arg-type]
        admin,
    )
    data = await admin_state.get_data()
    assert data["wallet_amount"] == CARD_DUE  # equals the card due, not the price
    assert data["wallet_user_id"] == buyer_id
    # The existing confirm screen, with the existing confirm button.
    confirm_markup = screen.answer.await_args_list[-1].kwargs["reply_markup"]
    ok = next(
        b for b in _buttons(confirm_markup) if b.callback_data.startswith(AdminCB.WALLET_OK)  # type: ignore[attr-defined]
    )

    # The other admin opened the same receipt before the first one confirmed.
    other = await session.scalar(select(User).where(User.telegram_id == OTHER_ADMIN_ID))
    other_state = _state(OTHER_ADMIN_ID)
    await admin_handlers.admin_late_receipt_credit(
        cb(credit_btn.callback_data, OTHER_ADMIN_ID),  # type: ignore[attr-defined]
        other_state,
        session,
        _settings(),  # type: ignore[arg-type]
        other,
    )
    assert (await other_state.get_data())["wallet_amount"] == CARD_DUE

    tap_bot = _bot()
    for _ in range(2):  # a double tap
        await admin_handlers.admin_wallet_apply(
            cb(ok.callback_data),  # type: ignore[attr-defined]
            admin_state,
            session,
            _settings(),  # type: ignore[arg-type]
            admin,
            tap_bot,  # type: ignore[arg-type]
        )
    assert await get_balance(session, buyer_id) == CARD_DUE
    entries = (await session.execute(select(WalletLedgerEntry))).scalars().all()
    assert [(e.amount_toman, e.reason) for e in entries] == [(CARD_DUE, "admin_manual_credit")]
    assert tap_bot.send_message.await_count == 1  # the customer was told once

    # The other admin's confirm for the same receipt cannot pay it out a second time.
    await admin_handlers.admin_wallet_apply(
        cb(ok.callback_data, OTHER_ADMIN_ID),  # type: ignore[attr-defined]
        other_state,
        session,
        _settings(),  # type: ignore[arg-type]
        other,
        tap_bot,  # type: ignore[arg-type]
    )
    assert await get_balance(session, buyer_id) == CARD_DUE
    assert tap_bot.send_message.await_count == 1
    # And a fresh tap on the receipt's button is refused too.
    await admin_handlers.admin_late_receipt_credit(
        cb(credit_btn.callback_data, OTHER_ADMIN_ID),  # type: ignore[attr-defined]
        _state(OTHER_ADMIN_ID),
        session,
        _settings(),  # type: ignore[arg-type]
        other,
    )
    assert order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.PENDING_PAYMENT, OrderStatus.ACTIVE])
async def test_only_an_expired_order_takes_a_late_receipt(
    session: AsyncSession, status: OrderStatus
) -> None:
    from services.payments import PaymentError, record_late_receipt

    buyer, order = await _expired_order(session)
    order.status = status
    await session.commit()

    with pytest.raises(PaymentError, match="invalid_status"):
        await record_late_receipt(session, order_id=order.id, user_id=buyer.id)
    assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 0
