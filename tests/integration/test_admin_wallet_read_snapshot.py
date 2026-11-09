"""Admin wallet screens must release read snapshots before Telegram calls.

The other session represents a scheduler or another administrator committing
while Telegram is slow. The database is the two_sessions temporary SQLite file.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.handlers import admin, admin_wallet
from bot.keyboards.admin import AdminCB
from bot.texts import texts
from database.base import end_read_transaction
from database.models import Order, OrderStatus, Plan, User, WalletLedgerEntry
from database.models.plan import DataLimitResetStrategy
from services.customer_lookup import adjust_wallet_manually, apply_wallet_adjustment
from services.payments import late_receipt_stamp

ADMIN_ID = 111
Factory = async_sessionmaker[AsyncSession]


def _settings() -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS=str(ADMIN_ID))


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    )


async def _seed(factory: Factory) -> tuple[int, int, int]:
    async with factory() as session:
        user = User(telegram_id=222, first_name="Ali", referral_code="SNAPW001")
        other = User(telegram_id=333, first_name="Worker", referral_code="SNAPW002")
        plan = Plan(
            name="snapshot",
            description="test",
            data_limit_gb=10,
            duration_days=30,
            price_toman=50_000,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[1],
            hwid_limit=0,
            is_active=True,
        )
        session.add_all([user, other, plan])
        await session.flush()
        user_id, other_id = int(user.id), int(other.id)
        order = Order(
            user_id=user_id,
            plan_id=plan.id,
            status=OrderStatus.EXPIRED,
            price_toman=50_000,
            wallet_applied_toman=10_000,
        )
        session.add(order)
        await session.flush()
        order_id = int(order.id)
        await session.commit()
        await apply_wallet_adjustment(
            session, user_id=user_id, amount_toman=50_000, admin_telegram_id=ADMIN_ID, stamp="seed"
        )
    return user_id, other_id, order_id


class TelegramSpy:
    """Record transaction state and let an independent worker commit during each call."""

    def __init__(self, session: AsyncSession, factory: Factory, other_id: int) -> None:
        self.session = session
        self.factory = factory
        self.other_id = other_id
        self.open_transactions: list[bool] = []
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def send(self, *args: Any, **kwargs: Any) -> None:
        self.open_transactions.append(self.session.in_transaction())
        self.calls.append((args, kwargs))
        async with self.factory() as worker:
            await worker.execute(
                update(User)
                .where(User.id == self.other_id)
                .values(first_name=f"Worker {len(self.calls)}")
            )
            await worker.commit()


def _callback(data: str, send: Any) -> SimpleNamespace:
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=ADMIN_ID), answer=send)


def _message(send: Any, *, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN_ID), message_id=77, text=text, answer=send
    )


async def test_wallet_prompt_sees_a_later_commit_instead_of_a_pinned_snapshot(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    user_id, _, _ = await _seed(factory)
    async with factory() as session:
        open_during_answer: list[bool] = []

        async def answer(*_args: Any, **_kwargs: Any) -> None:
            open_during_answer.append(session.in_transaction())
            async with factory() as worker:
                await apply_wallet_adjustment(
                    worker,
                    user_id=user_id,
                    amount_toman=12_000,
                    admin_telegram_id=ADMIN_ID,
                    stamp="concurrent",
                )

        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: None)
        state = _state()
        await admin_wallet.admin_wallet_start(
            _callback(f"{AdminCB.WALLET_ADD}{user_id}", answer),  # type: ignore[arg-type]
            state,
            session,
            _settings(),
            None,
        )
        balance = await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
        assert balance == 62_000, "the old snapshot hid the concurrent wallet credit"
        assert open_during_answer == [False]
        assert (await state.get_data())["wallet_user_id"] == user_id


@pytest.mark.parametrize("exists", [True, False])
async def test_wallet_prompt_releases_before_callback_and_screen(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    factory, _ = two_sessions
    user_id, other_id, _ = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: _message(spy.send))
        monkeypatch.setattr(admin_wallet, "_edit_or_answer_admin", spy.send)
        target_id = user_id if exists else 9999
        await admin_wallet.admin_wallet_start(
            _callback(f"{AdminCB.WALLET_ADD}{target_id}", spy.send),  # type: ignore[arg-type]
            _state(),
            session,
            _settings(),
            None,
        )
        assert spy.open_transactions == ([False, False] if exists else [False])
        if not exists:
            assert spy.calls[0][0][0] == texts.admin_user_not_found


@pytest.mark.parametrize("amount", ["25000", "-100000"])
async def test_wallet_amount_releases_before_confirm_or_refusal(
    two_sessions: tuple[Factory, str], amount: str
) -> None:
    factory, _ = two_sessions
    user_id, other_id, _ = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        state = _state()
        await state.update_data(wallet_user_id=user_id)
        await admin_wallet.admin_wallet_amount(
            _message(spy.send, text=amount), state, session, _settings(), None  # type: ignore[arg-type]
        )
        assert spy.open_transactions == [False]
        data = await state.get_data()
        assert data.get("wallet_amount") == (25_000 if amount == "25000" else None)


async def test_zero_confirmation_releases_before_editing_screen(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    user_id, other_id, _ = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: _message(spy.send))
        monkeypatch.setattr(admin_wallet, "_edit_or_answer_admin", spy.send)
        await admin_wallet.admin_wallet_zero_ask(
            _callback(f"{AdminCB.WALLET_ZERO}{user_id}", spy.send),  # type: ignore[arg-type]
            session,
            _settings(),
            None,
        )
        assert spy.open_transactions == [False, False]
        assert "۵۰٬۰۰۰" in spy.calls[-1][0][1]


async def test_zero_screen_rereads_a_balance_changed_during_the_callback(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    user_id, other_id, _ = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)

        async def answer(*_args: Any, **_kwargs: Any) -> None:
            assert not session.in_transaction()
            async with factory() as worker:
                await apply_wallet_adjustment(
                    worker,
                    user_id=user_id,
                    amount_toman=-50_000,
                    admin_telegram_id=999,
                    stamp="concurrent-zero",
                )

        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: _message(spy.send))
        monkeypatch.setattr(admin_wallet, "_edit_or_answer_admin", spy.send)
        await admin_wallet.admin_wallet_zero_ask(
            _callback(f"{AdminCB.WALLET_ZERO}{user_id}", answer),  # type: ignore[arg-type]
            session,
            _settings(),
            None,
        )
        assert spy.open_transactions == [False]
        assert spy.calls[0][0][1] == texts.admin_wallet_zero_already


@pytest.mark.parametrize("exists", [True, False])
async def test_late_receipt_releases_before_confirmation_or_refusal(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    factory, _ = two_sessions
    user_id, other_id, order_id = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: _message(spy.send))
        state = _state()
        await admin_wallet.admin_late_receipt_credit(
            _callback(f"{AdminCB.LATE_CREDIT}{order_id if exists else 9999}", spy.send),  # type: ignore[arg-type]
            state,
            session,
            _settings(),
            None,
        )
        assert spy.open_transactions == ([False, False] if exists else [False])
        if exists:
            data = await state.get_data()
            assert (data["wallet_user_id"], data["wallet_amount"]) == (user_id, 40_000)
        else:
            assert spy.calls[0][0][0] == texts.admin_late_receipt_gone


async def test_late_receipt_already_credited_releases_before_opening_confirmation(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id, other_id, order_id = await _seed(factory)
    async with factory() as seed:
        await apply_wallet_adjustment(
            seed,
            user_id=user_id,
            amount_toman=40_000,
            admin_telegram_id=ADMIN_ID,
            stamp=late_receipt_stamp(order_id),
        )
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        state = _state()
        await admin_wallet.admin_late_receipt_credit(
            _callback(f"{AdminCB.LATE_CREDIT}{order_id}", spy.send),  # type: ignore[arg-type]
            state,
            session,
            _settings(),
            None,
        )
        assert spy.open_transactions == [False]
        assert spy.calls[0][0][0] == texts.admin_late_receipt_already
        assert not await state.get_data()


async def test_wallet_apply_and_customer_card_send_after_committing_once(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = two_sessions
    user_id, other_id, _ = await _seed(factory)
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        monkeypatch.setattr(admin_wallet, "_tg_message", lambda _callback: _message(spy.send))
        monkeypatch.setattr(admin, "_edit_or_answer_admin", spy.send)
        state = _state()
        await state.update_data(wallet_user_id=user_id, wallet_amount=25_000, wallet_stamp="77")
        callback = _callback(f"{AdminCB.WALLET_OK}{user_id}", spy.send)
        await admin_wallet.admin_wallet_apply(
            callback, state, session, _settings(), None, SimpleNamespace(send_message=spy.send)  # type: ignore[arg-type]
        )
        assert spy.open_transactions == [False, False, False]
        await admin_wallet.admin_wallet_apply(
            callback, state, session, _settings(), None, SimpleNamespace(send_message=spy.send)  # type: ignore[arg-type]
        )
        assert spy.open_transactions == [False, False, False, False]
    async with factory() as check:
        balance = await check.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
        rows = (
            (
                await check.execute(
                    select(WalletLedgerEntry.amount_toman)
                    .where(WalletLedgerEntry.user_id == user_id)
                    .order_by(WalletLedgerEntry.id)
                )
            )
            .scalars()
            .all()
        )
    assert balance == 75_000
    assert list(rows) == [50_000, 25_000]


async def test_replayed_late_receipt_releases_before_refusal(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id, other_id, order_id = await _seed(factory)
    async with factory() as seed:
        await apply_wallet_adjustment(
            seed,
            user_id=user_id,
            amount_toman=40_000,
            admin_telegram_id=ADMIN_ID,
            stamp=late_receipt_stamp(order_id),
        )
    async with factory() as session:
        spy = TelegramSpy(session, factory, other_id)
        state = _state()
        await state.update_data(
            wallet_user_id=user_id, wallet_amount=40_000, wallet_stamp=late_receipt_stamp(order_id)
        )
        await admin_wallet.admin_wallet_apply(
            _callback(f"{AdminCB.WALLET_OK}{user_id}", spy.send),  # type: ignore[arg-type]
            state,
            session,
            _settings(),
            None,
            SimpleNamespace(send_message=spy.send),  # type: ignore[arg-type]
        )
        assert spy.open_transactions == [False]
        assert spy.calls[0][0][0] == texts.admin_late_receipt_already


async def test_wallet_confirm_rechecks_balance_after_a_concurrent_deduction(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id, _, _ = await _seed(factory)
    state = _state()
    await state.update_data(wallet_user_id=user_id)

    async with factory() as preview_session:

        async def another_admin_deducts(*_args: Any, **_kwargs: Any) -> None:
            assert not preview_session.in_transaction()
            async with factory() as worker:
                await apply_wallet_adjustment(
                    worker,
                    user_id=user_id,
                    amount_toman=-20_000,
                    admin_telegram_id=999,
                    stamp="concurrent",
                )

        await admin_wallet.admin_wallet_amount(
            _message(another_admin_deducts, text="-40000"),  # type: ignore[arg-type]
            state,
            preview_session,
            _settings(),
            None,
        )

    async with factory() as confirm_session:
        replies: list[str] = []

        async def record_answer(text: str, **_kwargs: Any) -> None:
            assert not confirm_session.in_transaction()
            replies.append(text)

        await admin_wallet.admin_wallet_apply(
            _callback(f"{AdminCB.WALLET_OK}{user_id}", record_answer),  # type: ignore[arg-type]
            state,
            confirm_session,
            _settings(),
            None,
            SimpleNamespace(send_message=record_answer),  # type: ignore[arg-type]
        )
        assert replies and "موجودی کافی نیست" in replies[0]

    async with factory() as check:
        balance = await check.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
        ledger = (
            (
                await check.execute(
                    select(WalletLedgerEntry.amount_toman)
                    .where(WalletLedgerEntry.user_id == user_id)
                    .order_by(WalletLedgerEntry.id)
                )
            )
            .scalars()
            .all()
        )
    assert balance == 30_000
    assert list(ledger) == [50_000, 20_000]


async def test_read_release_never_commits_an_unfinished_wallet_write(
    two_sessions: tuple[Factory, str],
) -> None:
    factory, _ = two_sessions
    user_id, _, _ = await _seed(factory)
    async with factory() as session:
        await adjust_wallet_manually(
            session,
            user_id=user_id,
            amount_toman=12_000,
            admin_telegram_id=ADMIN_ID,
            stamp="unfinished",
        )
        assert not await end_read_transaction(session)
        await session.rollback()
    async with factory() as check:
        balance = await check.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
        ledger = (
            (
                await check.execute(
                    select(WalletLedgerEntry.amount_toman).where(
                        WalletLedgerEntry.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert balance == 50_000
    assert list(ledger) == [50_000]
