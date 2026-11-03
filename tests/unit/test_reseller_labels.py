"""Labelling a service, which is the only handle a shop has on it.

Nobody remembers which `neo_a7f3` belongs to which walk-in customer. Three ways
in — buying, handing out a trial, relabelling afterwards — and all three go
through the same two questions. A single «اسم یا شماره» box got both halves
wrong: the name seeds the panel username, so «reza 09123456789» in one field
would put eleven digits into `reza_…`, and a phone is only findable by its last
four digits when it was stored as a phone.

The relabel path exists because services made before the bot asked, or before
the shop was a reseller, carry no name at all and «later» never comes without a
button.
"""

from __future__ import annotations

import pathlib
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
from bot.handlers.account import reseller_rename_start
from bot.handlers.customer_label import (
    SKIP_PHONE_CB,
    _finish,
    customer_name_entered,
    customer_phone_entered,
    customer_phone_skipped,
)
from bot.keyboards.account import AccountCB, service_detail_keyboard
from bot.states import ResellerStates
from database.base import Base
from database.models import User
from database.models.pasarguard_account import PasarGuardAccount

SETTINGS = SimpleNamespace(service_expire_warn_days=7, trial_enabled=True)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _shop(session: AsyncSession, tg: int, *, reseller: bool = True) -> User:
    user = User(telegram_id=tg, referral_code=f"R{tg}", is_reseller=reseller)
    session.add(user)
    await session.flush()
    return user


async def _service(
    session: AsyncSession, owner: User, n: int, label: str | None = None
) -> PasarGuardAccount:
    account = PasarGuardAccount(
        user_id=owner.id,
        panel_user_id=9000 + n,
        panel_username=f"neo_{n:03d}",
        subscription_url=f"https://panel.example.com/sub/neo_{n:03d}",
        display_name=label,
    )
    session.add(account)
    await session.flush()
    return account


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _callback(data: str, message: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=1), answer=AsyncMock(), message=message
    )


def _typed(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock(), edit_text=AsyncMock()
    )


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr("bot.handlers.account._tg_message", lambda _cb: msg)
    # One store group with the reseller trial on, so the label question is
    # the first thing the shop sees (no group picker).
    from services.trial import TrialOffer

    offer = TrialOffer(
        slug="unlimited",
        title_fa="نامحدود",
        data_limit_gb=1,
        duration_days=1,
        group_ids=[1],
        counts_legacy=True,
    )
    monkeypatch.setattr("bot.handlers.referral.list_trial_offers", AsyncMock(return_value=[offer]))
    return msg


async def _answer_label(
    state: FSMContext,
    session: AsyncSession,
    shop: User,
    *,
    name: str,
    phone: str | None,
) -> SimpleNamespace:
    """Walk both steps the way a shop does, and return the last message stub."""
    await customer_name_entered(_typed(name), state)  # type: ignore[arg-type]
    if phone is None:
        # `customer_phone_skipped` only unwraps `callback.message` and calls
        # this; a stub cannot satisfy its `isinstance(..., Message)` guard, so
        # the wiring is covered on its own below.
        message = _typed("")
        await _finish(
            message,  # type: ignore[arg-type]
            state,
            session,
            SETTINGS,  # type: ignore[arg-type]
            shop,
            phone=None,
        )
        return message
    message = _typed(phone)
    await customer_phone_entered(
        message,  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )
    return message


# --------------------------------------------------------------------------- #
# The button
# --------------------------------------------------------------------------- #


def test_only_a_reseller_is_offered_the_rename_button() -> None:
    """A customer's service is their own; «اسم مشتری» means nothing to them."""
    plain = service_detail_keyboard(1, has_link=True)
    shop = service_detail_keyboard(1, has_link=True, can_rename=True)
    assert not any("مشتری" in b.text for row in plain.inline_keyboard for b in row)
    assert any("مشتری" in b.text for row in shop.inline_keyboard for b in row)


def test_every_account_prefix_collision_is_defused_by_registration_order() -> None:
    """`AccountCB` grew four reseller entries and had no guard at all.

    Some of its prefixes genuinely nest — `acct:svc:refresh:` starts with
    `acct:svc:` — and that is *fine*, because aiogram dispatches to the first
    matching handler and the specific ones are registered first. What is not
    fine is adding a nested prefix whose handler lands **after** the generic
    one: it would never fire, and the symptom is a button that opens the wrong
    screen rather than an error.

    Source order is registration order for a module-level router, so the file
    itself is the thing to check.
    """
    import re

    source = pathlib.Path("bot/handlers/account.py").read_text(encoding="utf-8")
    positions: dict[str, int] = {}
    for match in re.finditer(r"F\.data\.startswith\(AccountCB\.(\w+)\)", source):
        positions.setdefault(match.group(1), match.start())

    constants = {
        name: value
        for name, value in vars(AccountCB).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    checked = 0
    for specific, general in [
        (a, b)
        for a in positions
        for b in positions
        if a != b and constants[a].startswith(constants[b])
    ]:
        checked += 1
        assert positions[specific] < positions[general], (
            f"{specific} ({constants[specific]}) nests inside {general} "
            f"({constants[general]}) but is registered after it, so it can never fire"
        )
    assert checked, "the nesting this guards against no longer exists — keep or delete"


def test_the_new_reseller_prefixes_do_not_nest_in_anything() -> None:
    """The four added for the reseller desk stand on their own."""
    constants = {
        name: value
        for name, value in vars(AccountCB).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    for name in ("RS_PAGE", "RS_SEARCH", "RS_CLEAR", "RS_RENAME"):
        value = constants[name]
        for other, other_value in constants.items():
            if other == name or not other_value.endswith(":"):
                continue
            assert not value.startswith(other_value), f"{name} nests inside {other}"


# --------------------------------------------------------------------------- #
# The two steps
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_renaming_asks_for_the_name_then_the_phone(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    shop = await _shop(session, 700)
    account = await _service(session, shop, 1, None)
    await session.commit()

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{account.id}"),  # type: ignore[arg-type]
        state,
        session,
        shop,
    )
    assert await state.get_state() == ResellerStates.waiting_customer_name.state

    await customer_name_entered(_typed("rezaei"), state)  # type: ignore[arg-type]
    assert await state.get_state() == ResellerStates.waiting_customer_phone.state

    await customer_phone_entered(
        _typed("09121112233"),  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )
    await session.refresh(account)
    assert account.display_name == "rezaei | 09121112233"
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_the_phone_step_can_be_skipped(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """Without the button, whatever they type becomes an unsearchable «phone»."""
    shop = await _shop(session, 700)
    account = await _service(session, shop, 1, None)
    await session.commit()

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{account.id}"),  # type: ignore[arg-type]
        state,
        session,
        shop,
    )
    await _answer_label(state, session, shop, name="rezaei", phone=None)
    await session.refresh(account)
    assert account.display_name == "rezaei"
    assert "|" not in account.display_name


@pytest.mark.asyncio
async def test_the_phone_is_stored_in_ascii_however_it_was_typed(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """«۰۹۱۲ ۳۴۵ ۶۷۸۹» and «0912-345-6789» are one number written two ways.

    Filed as typed, neither is findable by its last four digits — SQL `LIKE`
    does not know ۶ and 6 are the same digit.
    """
    shop = await _shop(session, 700)
    account = await _service(session, shop, 1, None)
    await session.commit()

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{account.id}"),  # type: ignore[arg-type]
        state,
        session,
        shop,
    )
    await _answer_label(state, session, shop, name="karimi", phone="۰۹۱۲ ۳۴۵ ۶۷۸۹")
    await session.refresh(account)
    assert account.display_name == "karimi | 09123456789"


@pytest.mark.asyncio
async def test_a_service_made_before_the_bot_asked_can_be_named(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """The owner's report: «مشتری قدیمی با فرمت قبل ثبت شده و امکان ثبت شماره ندارد»."""
    shop = await _shop(session, 700)
    legacy = await _service(session, shop, 2, None)
    await session.commit()
    assert legacy.display_name is None

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{legacy.id}"),  # type: ignore[arg-type]
        state,
        session,
        shop,
    )
    await _answer_label(state, session, shop, name="karimi", phone="09122223344")
    await session.refresh(legacy)
    assert legacy.display_name == "karimi | 09122223344"


@pytest.mark.asyncio
async def test_a_bad_phone_keeps_the_step_open_with_its_button(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    shop = await _shop(session, 700)
    account = await _service(session, shop, 6, "قبلی")
    await session.commit()

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{account.id}"),  # type: ignore[arg-type]
        state,
        session,
        shop,
    )
    await customer_name_entered(_typed("rezaei"), state)  # type: ignore[arg-type]
    message = _typed("بعداً می‌فرستم")
    await customer_phone_entered(
        message,  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )
    await session.refresh(account)
    assert account.display_name == "قبلی", "nothing is written until both steps answer"
    assert await state.get_state() == ResellerStates.waiting_customer_phone.state
    assert message.answer.await_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["   ", "رضایی"])
async def test_a_name_the_panel_cannot_hold_keeps_the_first_step_open(
    raw: str, session: AsyncSession, screen: SimpleNamespace
) -> None:
    """A Persian name sanitizes to nothing, so it cannot seed a username."""
    state = _state()
    message = _typed(raw)
    await customer_name_entered(message, state)  # type: ignore[arg-type]
    assert await state.get_state() != ResellerStates.waiting_customer_phone.state
    assert message.answer.await_count == 1


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_shop_cannot_rename_another_shops_service(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """The id comes from a callback, which anyone can forge by forwarding."""
    mine = await _shop(session, 700)
    theirs = await _shop(session, 701)
    account = await _service(session, theirs, 3, "مال اون")
    await session.commit()

    state = _state()
    callback = _callback(f"{AccountCB.RS_RENAME}{account.id}")
    await reseller_rename_start(
        callback,  # type: ignore[arg-type]
        state,
        session,
        mine,
    )
    assert await state.get_state() is None, "the flow must not even start"
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    await session.refresh(account)
    assert account.display_name == "مال اون"


@pytest.mark.asyncio
async def test_saving_is_checked_again_not_only_at_the_prompt(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """State can be steered; ownership is re-checked where the write happens."""
    mine = await _shop(session, 700)
    theirs = await _shop(session, 701)
    account = await _service(session, theirs, 4, "مال اون")
    await session.commit()

    state = _state()
    await state.set_state(ResellerStates.waiting_customer_name)
    await state.update_data(label_flow="rename", rename_account_id=account.id)
    await _answer_label(state, session, mine, name="stolen", phone=None)
    await session.refresh(account)
    assert account.display_name == "مال اون"


@pytest.mark.asyncio
async def test_a_plain_customer_cannot_rename_at_all(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    customer = await _shop(session, 702, reseller=False)
    account = await _service(session, customer, 5, "سرویس خودم")
    await session.commit()

    state = _state()
    await reseller_rename_start(
        _callback(f"{AccountCB.RS_RENAME}{account.id}"),  # type: ignore[arg-type]
        state,
        session,
        customer,
    )
    assert await state.get_state() is None


# --------------------------------------------------------------------------- #
# The trial asks who it is for
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_trial_asks_before_it_creates_anything(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """It used to create an unlabelled service and never ask.

    A label added «later» never is, so the question comes first — and nothing
    is written until it is answered.
    """
    from bot.handlers.referral import render_trial
    from services.reseller import set_rate

    shop = await _shop(session, 700)
    await set_rate(session, shop.id, trial_quota_monthly=2)
    await session.commit()

    state = _state()
    message = _typed("")
    await render_trial(
        message,  # type: ignore[arg-type]
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
        state,
    )
    assert await state.get_state() == ResellerStates.waiting_customer_name.state
    said = message.answer.await_args.args[0]
    assert "کدوم مشتری" in said
    assert (
        await session.scalar(
            select(func.count())
            .select_from(PasarGuardAccount)
            .where(PasarGuardAccount.user_id == shop.id)
        )
        == 0
    ), "nothing may be created before the question is answered"


@pytest.mark.asyncio
async def test_the_prompt_says_whether_this_one_is_free(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """«چرا این بار پول کم شد؟» is the question a silent charge produces."""
    from bot.handlers.referral import render_trial
    from services.reseller import set_rate

    shop = await _shop(session, 701)
    await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=5000)
    await session.commit()

    message = _typed("")
    await render_trial(
        message,  # type: ignore[arg-type]
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
        _state(),
    )
    said = message.answer.await_args.args[0]
    assert "۵٬۰۰۰" in said, "they must know the price before they name the customer"


@pytest.mark.asyncio
async def test_an_exhausted_shop_is_told_before_being_asked(
    session: AsyncSession, screen: SimpleNamespace
) -> None:
    """Asking for a name and then refusing would waste the shop's time."""
    from bot.handlers.referral import render_trial
    from services.reseller import set_rate

    shop = await _shop(session, 702)
    await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=0)
    await session.commit()

    state = _state()
    message = _typed("")
    await render_trial(
        message,  # type: ignore[arg-type]
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
        state,
    )
    assert await state.get_state() is None
    assert "سهمیه" in message.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# The skip button's own wiring
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_unreachable_chat_says_so_instead_of_doing_nothing(
    session: AsyncSession,
) -> None:
    """The message is older than Telegram lets the bot touch.

    A «شماره ندارم» that silently does nothing reads as a broken bot, and the
    shop has a customer standing in front of them.
    """
    shop = await _shop(session, 704)
    await session.commit()
    state = _state()
    await state.set_state(ResellerStates.waiting_customer_phone)
    await state.update_data(label_flow="rename", rs_customer_name="rezaei")

    callback = _callback(SKIP_PHONE_CB, None)
    await customer_phone_skipped(
        callback,  # type: ignore[arg-type]
        state,
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_the_skip_button_reports_no_phone(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the one thing the handler decides: `phone=None`, not `""`."""
    seen: dict[str, object] = {}

    async def _spy(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("bot.handlers.customer_label._finish", _spy)
    monkeypatch.setattr("bot.handlers.customer_label.Message", SimpleNamespace)

    shop = await _shop(session, 705)
    await session.commit()
    callback = _callback(SKIP_PHONE_CB, _typed(""))
    await customer_phone_skipped(
        callback,  # type: ignore[arg-type]
        _state(),
        session,
        SETTINGS,  # type: ignore[arg-type]
        shop,
    )
    assert seen == {"phone": None}
