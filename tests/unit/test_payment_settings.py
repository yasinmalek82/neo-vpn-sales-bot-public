"""The payment card the owner edits in Telegram (P4): DB per field, else env."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.handlers import payment as payment_handlers
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from bot.texts import texts
from database.base import Base
from services.payment_settings import (
    PaymentSettingsError,
    clear_to_env,
    format_card_number,
    get_payment_info,
    normalize_card_number,
    save_payment_field,
)

OWNER_ID = 4242
OPS_ID = 9999
ENV_CARD = "6037991111111111"
NEW_CARD = "6219861234567890"


def _settings(**overrides: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "admin_telegram_ids": [OWNER_ID, OPS_ID],
        "store_owner_telegram_ids": [OWNER_ID],
        "payment_card_number": ENV_CARD,
        "payment_card_holder_name": "env holder",
        "payment_bank_name": "env bank",
        "payment_instructions": "env note",
        "bank_sms_enabled": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---- service -------------------------------------------------------------


async def test_env_is_used_when_nothing_is_stored(session: AsyncSession) -> None:
    info = await get_payment_info(session, _settings())  # type: ignore[arg-type]
    assert info.card_number == ENV_CARD
    assert info.from_env == {"card_number", "card_holder_name", "bank_name", "instructions"}
    assert info.has_override is False


async def test_db_value_beats_env_per_field(session: AsyncSession) -> None:
    await save_payment_field(session, "card_number", NEW_CARD)
    await save_payment_field(session, "bank_name", "ملت")
    info = await get_payment_info(session, _settings())  # type: ignore[arg-type]
    assert info.card_number == NEW_CARD
    assert info.bank_name == "ملت"
    # the two fields the owner did not touch still come from env
    assert info.card_holder_name == "env holder"
    assert info.instructions == "env note"
    assert info.from_env == {"card_holder_name", "instructions"}
    assert info.has_override is True


@pytest.mark.parametrize(
    "raw",
    [
        "6219861234567890",
        "۶۲۱۹۸۶۱۲۳۴۵۶۷۸۹۰",
        "٦٢١٩٨٦١٢٣٤٥٦٧٨٩٠",
        "6219 8612 3456 7890",
        "6219-8612-3456-7890",
        "\u200f۶۲۱۹ ۸۶۱۲ ۳۴۵۶ ۷۸۹۰\u200e",
        "\u202a6219 8612–3456—7890\u202c",
    ],
)
def test_card_number_is_normalized(raw: str) -> None:
    assert normalize_card_number(raw) == NEW_CARD


@pytest.mark.parametrize(
    "raw", ["", "621986123456789", "62198612345678901", "6219x61234567890", "abcd" * 4]
)
def test_card_number_must_be_exactly_16_digits(raw: str) -> None:
    with pytest.raises(PaymentSettingsError) as exc:
        normalize_card_number(raw)
    assert exc.value.code == "card_invalid"


def test_card_is_shown_in_groups_of_four() -> None:
    assert format_card_number("6037991234567890") == "6037 9912 3456 7890"


async def test_fifteen_digits_is_refused_and_nothing_is_written(session: AsyncSession) -> None:
    with pytest.raises(PaymentSettingsError):
        await save_payment_field(session, "card_number", "603799123456789")
    info = await get_payment_info(session, _settings())  # type: ignore[arg-type]
    assert info.card_number == ENV_CARD


@pytest.mark.parametrize(
    ("field", "limit"),
    [("card_holder_name", 64), ("bank_name", 64), ("instructions", 300)],
)
async def test_text_limits(session: AsyncSession, field: str, limit: int) -> None:
    await save_payment_field(session, field, "x" * limit)
    with pytest.raises(PaymentSettingsError) as exc:
        await save_payment_field(session, field, "x" * (limit + 1))
    assert exc.value.code == "too_long"
    with pytest.raises(PaymentSettingsError):
        await save_payment_field(session, field, "   ")


async def test_clear_to_env_restores_env(session: AsyncSession) -> None:
    await save_payment_field(session, "card_number", NEW_CARD)
    await save_payment_field(session, "card_holder_name", "علی")
    await clear_to_env(session, admin_user_id=1)
    info = await get_payment_info(session, _settings())  # type: ignore[arg-type]
    assert info.card_number == ENV_CARD
    assert info.card_holder_name == "env holder"
    assert info.has_override is False


async def test_clear_without_a_row_is_a_no_op(session: AsyncSession) -> None:
    await clear_to_env(session)


# ---- customer text -------------------------------------------------------


async def test_customer_payment_text_shows_the_db_card(session: AsyncSession) -> None:
    await save_payment_field(session, "card_number", NEW_CARD)
    await save_payment_field(session, "bank_name", "ملت")
    info = await get_payment_info(session, _settings())  # type: ignore[arg-type]
    text = payment_handlers.format_payment_instructions(info, order_id=7, amount_toman=150000)
    assert NEW_CARD in text
    assert ENV_CARD not in text
    assert "ملت" in text
    assert "env holder" in text  # untouched field still falls back to env


async def test_payment_screen_shows_new_card_right_after_save_without_restart(
    session: AsyncSession,
) -> None:
    settings = _settings()
    msg = SimpleNamespace(answer=AsyncMock())
    state = _state()
    await payment_handlers.begin_payment_wait(
        msg, state, settings, session=session, order_id=1, amount_toman=1000  # type: ignore[arg-type]
    )
    assert ENV_CARD in msg.answer.await_args.args[0]

    await save_payment_field(session, "card_number", NEW_CARD)
    await session.commit()
    await payment_handlers.begin_payment_wait(
        msg, state, settings, session=session, order_id=1, amount_toman=1000  # type: ignore[arg-type]
    )
    shown = msg.answer.await_args.args[0]
    assert NEW_CARD in shown
    assert ENV_CARD not in shown
    assert "۱۰٬۰۰۰</b> ریال" in shown
    keyboard = msg.answer.await_args.kwargs["reply_markup"]
    copied = {
        button.copy_text.text
        for row in keyboard.inline_keyboard
        for button in row
        if button.copy_text is not None
    }
    assert copied == {NEW_CARD, "10000"}


def test_no_customer_read_site_still_reads_env_directly() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in ("bot/handlers/payment.py", "bot/handlers/purchase.py", "services/payments.py"):
        source = (root / rel).read_text()
        for name in (
            "settings.payment_card_number",
            "settings.payment_card_holder_name",
            "settings.payment_bank_name",
            "settings.payment_instructions",
        ):
            assert name not in source, f"{rel} reads {name} instead of get_payment_info"
    assert "session" in inspect.signature(payment_handlers.begin_payment_wait).parameters


# ---- store hub screen ----------------------------------------------------


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _screen() -> SimpleNamespace:
    return SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())


def _cb(data: str, user_id: int = OWNER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(), message=None
    )


def _typed(text: str, user_id: int = OWNER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


def _cbs(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    msg = _screen()
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: msg)
    return msg


def _last(mock: AsyncMock) -> tuple[str, object]:
    call = mock.await_args_list[-1]
    return call.args[0], call.kwargs.get("reply_markup") or call.args[1]


async def test_screen_marks_env_fields_and_hides_reset_until_override(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    settings = _settings()
    await ha.store_pay_display(_cb(StoreCB.PAY), settings, None, session, _state())  # type: ignore[arg-type]
    body, kb = _last(screen.edit_text)
    assert body.count("(از .env)") == 4
    assert format_card_number(ENV_CARD) in body
    assert StoreCB.PAY_CLEAR not in _cbs(kb)
    assert {"🔢 شماره کارت", "👤 صاحب کارت", "🏛 بانک", "📝 توضیح"} <= set(_labels(kb))

    await save_payment_field(session, "bank_name", "ملت")
    await session.commit()
    await ha.store_pay_display(_cb(StoreCB.PAY), settings, None, session, _state())  # type: ignore[arg-type]
    body, kb = _last(screen.edit_text)
    assert body.count("(از .env)") == 3
    assert StoreCB.PAY_CLEAR in _cbs(kb)


async def test_card_needs_a_confirm_tap_before_it_is_saved(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    settings = _settings()
    state = _state()
    await ha.store_pay_edit(_cb(f"{StoreCB.PAY_EDIT}card_number"), settings, None, state)  # type: ignore[arg-type]
    assert await state.get_state() == AdminStates.store_waiting_pay_field.state

    # 15 digits: refused, still waiting, nothing stored
    bad = _typed("۶۲۱۹ ۸۶۱۲ ۳۴۵۶ ۷۸۹")
    await ha.store_pay_input(bad, state, settings, None, session)  # type: ignore[arg-type]
    assert texts.admin_store_pay_errors["card_invalid"] in bad.answer.await_args.args[0]
    assert (await get_payment_info(session, settings)).card_number == ENV_CARD  # type: ignore[arg-type]

    good = _typed("۶۲۱۹-۸۶۱۲-۳۴۵۶-۷۸۹۰")
    await ha.store_pay_input(good, state, settings, None, session)  # type: ignore[arg-type]
    shown, kb = _last(good.answer)
    assert "6219 8612 3456 7890" in shown
    assert StoreCB.PAY_CARD_OK in _cbs(kb)
    # typed, not yet confirmed: customers still see the old card
    assert (await get_payment_info(session, settings)).card_number == ENV_CARD  # type: ignore[arg-type]

    await ha.store_pay_card_save(_cb(StoreCB.PAY_CARD_OK), settings, None, session, state)  # type: ignore[arg-type]
    assert (await get_payment_info(session, settings)).card_number == NEW_CARD  # type: ignore[arg-type]
    assert await state.get_state() is None


async def test_text_field_saves_directly_and_rerenders(
    session: AsyncSession,
) -> None:
    settings = _settings()
    state = _state()
    await ha.store_pay_edit(_cb(f"{StoreCB.PAY_EDIT}card_holder_name"), settings, None, state)  # type: ignore[arg-type]
    msg = _typed("علی رضایی")
    await ha.store_pay_input(msg, state, settings, None, session)  # type: ignore[arg-type]
    body, _kb = _last(msg.answer)
    assert "علی رضایی" in body
    assert (await get_payment_info(session, settings)).card_holder_name == "علی رضایی"  # type: ignore[arg-type]


async def test_ops_admin_cannot_edit(session: AsyncSession, screen: SimpleNamespace) -> None:
    settings = _settings()
    state = _state()
    cb = _cb(f"{StoreCB.PAY_EDIT}card_number", user_id=OPS_ID)
    await ha.store_pay_edit(cb, settings, None, state)  # type: ignore[arg-type]
    assert await state.get_state() is None
    cb.answer.assert_awaited_once()

    await ha.store_pay_display(_cb(StoreCB.PAY, user_id=OPS_ID), settings, None, session, state)  # type: ignore[arg-type]
    _body, kb = _last(screen.edit_text)
    assert not any(c.startswith(StoreCB.PAY_EDIT) for c in _cbs(kb))


async def test_reset_to_env_asks_first_and_then_restores(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    settings = _settings()
    await save_payment_field(session, "card_number", NEW_CARD)
    await session.commit()
    await ha.store_pay_clear_confirm(_cb(StoreCB.PAY_CLEAR), settings, None, session, _state())  # type: ignore[arg-type]
    assert StoreCB.PAY_CLEAR_OK in _cbs(_last(screen.edit_text)[1])
    assert (await get_payment_info(session, settings)).card_number == NEW_CARD  # type: ignore[arg-type]

    await ha.store_pay_clear(_cb(StoreCB.PAY_CLEAR_OK), settings, None, session, _state())  # type: ignore[arg-type]
    assert (await get_payment_info(session, settings)).card_number == ENV_CARD  # type: ignore[arg-type]


def test_payment_callbacks_fit_and_do_not_swallow_each_other() -> None:
    values = {n: v for n, v in vars(StoreCB).items() if n.isupper() and isinstance(v, str)}
    new = {n: v for n, v in values.items() if n.startswith("PAY")}
    assert set(new) == {
        "PAY",
        "PAY_EDIT",
        "PAY_CARD_OK",
        "PAY_CLEAR",
        "PAY_CLEAR_OK",
        "PAY_MODE_ON",
        "PAY_MODE_OFF",
    }
    for name, value in new.items():
        assert len(value.encode()) <= 64
        for other_name, other in values.items():
            if other_name == name:
                continue
            if other.endswith(":"):
                assert not value.startswith(other), f"{name} swallowed by {other_name}"
            if value.endswith(":"):
                assert not other.startswith(value), f"{other_name} swallowed by {name}"
