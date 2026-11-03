"""Customer search and manual wallet adjustment.

Search matters because the panel could only look up an order id, while a
customer writing in gives a @username or nothing. Manual adjustment matters
because it is real money and must go through the same immutable ledger as
every automatic credit.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import Order, OrderStatus, Plan, User
from database.models.pasarguard_account import PasarGuardAccount
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.customer_lookup import (
    MANUAL_CREDIT_REASON,
    MANUAL_DEBIT_REASON,
    USER_FILTER_LABELS,
    ManualAdjustError,
    UserFilter,
    adjust_wallet_manually,
    find_customers,
    get_customer_card,
    list_users,
    reset_trial,
    zero_wallet,
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(
    session: AsyncSession,
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    balance: int = 0,
) -> User:
    user = User(
        telegram_id=telegram_id,
        referral_code=f"REF{telegram_id}",
        username=username,
        first_name=first_name,
    )
    user.wallet_balance_toman = balance
    session.add(user)
    await session.flush()
    return user


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_finds_by_telegram_id(session: AsyncSession) -> None:
    await _user(session, 560110047, username="yasin")
    await session.commit()
    assert [u.telegram_id for u in await find_customers(session, "560110047")] == [560110047]


@pytest.mark.asyncio
async def test_finds_by_username_with_or_without_at(session: AsyncSession) -> None:
    await _user(session, 1, username="yasin_mst")
    await session.commit()
    for query in ("yasin_mst", "@yasin_mst", "YASIN_MST", "t.me/yasin_mst"):
        assert len(await find_customers(session, query)) == 1, query


@pytest.mark.asyncio
async def test_finds_by_partial_name(session: AsyncSession) -> None:
    """A support message rarely carries an exact handle."""
    await _user(session, 1, first_name="محمدرضا")
    await session.commit()
    assert len(await find_customers(session, "محمد")) == 1


@pytest.mark.asyncio
async def test_finds_by_referral_code(session: AsyncSession) -> None:
    await _user(session, 42)
    await session.commit()
    assert [u.telegram_id for u in await find_customers(session, "ref42")] == [42]


@pytest.mark.asyncio
async def test_an_empty_query_returns_nothing_rather_than_everyone(
    session: AsyncSession,
) -> None:
    await _user(session, 1, username="a")
    await _user(session, 2, username="b")
    await session.commit()
    assert await find_customers(session, "   ") == []


@pytest.mark.asyncio
async def test_no_match_is_an_empty_list_not_an_error(session: AsyncSession) -> None:
    assert await find_customers(session, "nobody") == []


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_card_gathers_wallet_services_and_spend(session: AsyncSession) -> None:
    user = await _user(session, 1, username="yasin", balance=25_000)
    plan = Plan(name="p", data_limit_gb=50, duration_days=30, price_toman=100_000)
    session.add(plan)
    await session.flush()
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_username="yasin_ab12",
            panel_user_id=7,
            display_name="سرویس اصلی",
            subscription_url="https://panel.example/sub/xyz",
        )
    )
    session.add_all(
        [
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=150_000,
                discount_toman=0,
            ),
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=90_000,
                discount_toman=0,
            ),
            # Never paid — must not count toward spend.
            Order(
                user_id=user.id,
                plan_id=plan.id,
                status=OrderStatus.REJECTED,
                price_toman=999_000,
                discount_toman=0,
            ),
        ]
    )
    await session.commit()

    card = await get_customer_card(session, user.id)
    assert card.wallet_balance_toman == 25_000
    assert [s.display_name for s in card.services] == ["سرویس اصلی"]
    assert card.orders_total == 3
    assert card.orders_paid == 2
    assert card.spent_toman == 240_000


@pytest.mark.asyncio
async def test_card_for_a_customer_with_nothing(session: AsyncSession) -> None:
    user = await _user(session, 1)
    await session.commit()
    card = await get_customer_card(session, user.id)
    assert card.services == []
    assert card.orders_total == 0
    assert card.spent_toman == 0
    assert card.last_order_at is None


# --------------------------------------------------------------------------- #
# Manual wallet adjustment
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_manual_credit_moves_the_balance_and_leaves_a_trail(
    session: AsyncSession,
) -> None:
    user = await _user(session, 1, balance=10_000)
    await session.commit()

    entry = await adjust_wallet_manually(
        session, user_id=user.id, amount_toman=50_000, admin_telegram_id=999, stamp="s1"
    )
    await session.commit()

    assert entry.entry_type is WalletEntryType.credit
    assert entry.reason == MANUAL_CREDIT_REASON
    assert "admin=999" in (entry.note or "")
    assert int(user.wallet_balance_toman) == 60_000


@pytest.mark.asyncio
async def test_manual_debit_reduces_the_balance(session: AsyncSession) -> None:
    user = await _user(session, 1, balance=80_000)
    await session.commit()

    entry = await adjust_wallet_manually(
        session, user_id=user.id, amount_toman=-30_000, admin_telegram_id=999, stamp="s1"
    )
    await session.commit()

    assert entry.entry_type is WalletEntryType.debit
    assert entry.reason == MANUAL_DEBIT_REASON
    assert int(user.wallet_balance_toman) == 50_000


@pytest.mark.asyncio
async def test_a_debit_larger_than_the_balance_is_refused(
    session: AsyncSession,
) -> None:
    """A negative wallet would be a debt the customer never agreed to."""
    user = await _user(session, 1, balance=20_000)
    await session.commit()

    with pytest.raises(ManualAdjustError):
        await adjust_wallet_manually(
            session, user_id=user.id, amount_toman=-50_000, admin_telegram_id=999, stamp="s1"
        )
    assert int(user.wallet_balance_toman) == 20_000


@pytest.mark.asyncio
async def test_the_same_confirmation_tapped_twice_credits_once(
    session: AsyncSession,
) -> None:
    user = await _user(session, 1, balance=0)
    await session.commit()

    for _ in range(2):
        await adjust_wallet_manually(
            session, user_id=user.id, amount_toman=25_000, admin_telegram_id=999, stamp="s1"
        )
        await session.commit()

    assert int(user.wallet_balance_toman) == 25_000
    entries = (await session.execute(select(WalletLedgerEntry))).scalars().all()
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_a_deliberate_second_top_up_is_still_possible(
    session: AsyncSession,
) -> None:
    """Idempotency must protect against double-taps, not against real repeats."""
    user = await _user(session, 1, balance=0)
    await session.commit()

    for stamp in ("s1", "s2"):
        await adjust_wallet_manually(
            session, user_id=user.id, amount_toman=25_000, admin_telegram_id=999, stamp=stamp
        )
        await session.commit()

    assert int(user.wallet_balance_toman) == 50_000


@pytest.mark.asyncio
async def test_zero_and_missing_user_are_refused(session: AsyncSession) -> None:
    user = await _user(session, 1)
    await session.commit()

    with pytest.raises(ManualAdjustError):
        await adjust_wallet_manually(
            session, user_id=user.id, amount_toman=0, admin_telegram_id=999, stamp="s1"
        )
    with pytest.raises(ManualAdjustError):
        await adjust_wallet_manually(
            session, user_id=999_999, amount_toman=1000, admin_telegram_id=999, stamp="s1"
        )


# --------------------------------------------------------------------------- #
# Browse list
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_deployment_lists_nothing_without_blowing_up(
    session: AsyncSession,
) -> None:
    page = await list_users(session)
    assert page.rows == []
    assert page.total == 0
    assert page.has_prev is False
    assert page.has_next is False


@pytest.mark.asyncio
async def test_list_is_newest_first_and_counts_everyone(session: AsyncSession) -> None:
    for i in range(3):
        await _user(session, 5000 + i, username=f"u{i}", first_name=f"User {i}")
    await session.commit()

    page = await list_users(session)
    assert page.total == 3
    assert [r.telegram_id for r in page.rows] == [5002, 5001, 5000]
    assert page.rows[0].label == "User 2"


@pytest.mark.asyncio
async def test_paging_walks_every_user_exactly_once(session: AsyncSession) -> None:
    for i in range(25):
        await _user(session, 6000 + i, username=f"p{i}")
    await session.commit()

    seen: list[int] = []
    offset = 0
    for _ in range(10):  # generous bound; the loop must end well before this
        page = await list_users(session, offset=offset, limit=10)
        seen.extend(r.telegram_id for r in page.rows)
        if not page.has_next:
            break
        offset = page.next_offset

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a user appeared on two pages"


@pytest.mark.asyncio
async def test_prev_offset_steps_back_a_full_page_from_a_partial_last_page(
    session: AsyncSession,
) -> None:
    """The last page holds 5 of 25 rows.

    Stepping back by the row count would move 5 instead of 10 and leave a
    sliding window that never reaches the start.
    """
    for i in range(25):
        await _user(session, 7000 + i, username=f"q{i}")
    await session.commit()

    last = await list_users(session, offset=20, limit=10)
    assert len(last.rows) == 5
    assert last.has_next is False
    assert last.prev_offset == 10


@pytest.mark.asyncio
async def test_row_carries_wallet_service_count_and_flags(session: AsyncSession) -> None:
    user = await _user(session, 8001, username="rich", first_name="Rich", balance=250_000)
    user.is_banned = True
    for i in range(2):
        session.add(
            PasarGuardAccount(
                user_id=user.id,
                panel_user_id=500 + i,
                panel_username=f"rich_{i}",
                subscription_url=f"https://panel.example.com/sub/rich_{i}",
            )
        )
    await _user(session, 8002, username="poor")
    await session.commit()

    page = await list_users(session)
    by_tg = {r.telegram_id: r for r in page.rows}
    assert by_tg[8001].wallet_balance_toman == 250_000
    assert by_tg[8001].service_count == 2
    assert by_tg[8001].is_banned is True
    # A user with no accounts must read as zero, not go missing from the page.
    assert by_tg[8002].service_count == 0
    assert by_tg[8002].is_banned is False


@pytest.mark.asyncio
async def test_label_falls_back_through_what_telegram_gave_us(
    session: AsyncSession,
) -> None:
    await _user(session, 9001, username="handle_only")
    await _user(session, 9002)
    await session.commit()

    page = await list_users(session)
    labels = {r.telegram_id: r.label for r in page.rows}
    assert labels[9001] == "@handle_only"
    assert labels[9002] == "9002", "a user with no name at all must still be pickable"


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


async def _account(session: AsyncSession, user: User, n: int) -> None:
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=1000 + n,
            panel_username=f"svc_{n}",
            subscription_url=f"https://panel.example.com/sub/svc_{n}",
        )
    )


@pytest_asyncio.fixture
async def mixed_users(session: AsyncSession) -> dict[str, User]:
    rich = await _user(session, 100, username="rich", balance=50_000)
    broke = await _user(session, 101, username="broke")
    banned = await _user(session, 102, username="banned")
    banned.is_banned = True
    referrer = await _user(session, 103, username="referrer")
    await session.flush()
    invitee = await _user(session, 104, username="invitee")
    invitee.referred_by_id = referrer.id
    await _account(session, rich, 1)
    await _account(session, rich, 2)  # two services, one user
    await _account(session, banned, 3)
    await session.commit()
    return {
        "rich": rich,
        "broke": broke,
        "banned": banned,
        "referrer": referrer,
        "invitee": invitee,
    }


@pytest.mark.asyncio
async def test_all_filter_shows_everyone(session: AsyncSession, mixed_users) -> None:
    page = await list_users(session, user_filter=UserFilter.ALL)
    assert page.total == 5


@pytest.mark.asyncio
async def test_balance_filter(session: AsyncSession, mixed_users) -> None:
    page = await list_users(session, user_filter=UserFilter.WITH_BALANCE)
    assert [r.telegram_id for r in page.rows] == [100]
    assert page.total == 1


@pytest.mark.asyncio
async def test_banned_filter(session: AsyncSession, mixed_users) -> None:
    page = await list_users(session, user_filter=UserFilter.BANNED)
    assert [r.telegram_id for r in page.rows] == [102]


@pytest.mark.asyncio
async def test_referrals_filter_finds_the_referrer_not_the_invitee(
    session: AsyncSession, mixed_users
) -> None:
    page = await list_users(session, user_filter=UserFilter.WITH_REFERRALS)
    assert [r.telegram_id for r in page.rows] == [103]


@pytest.mark.asyncio
async def test_a_user_with_two_services_appears_once(session: AsyncSession, mixed_users) -> None:
    """The join trap.

    Filtering «سرویس‌دار» with a plain join would list the two-service customer
    twice and make the page count lie. It is written as a correlated EXISTS for
    exactly this reason.
    """
    page = await list_users(session, user_filter=UserFilter.WITH_SERVICE)
    ids = [r.telegram_id for r in page.rows]
    assert sorted(ids) == [100, 102]
    assert len(ids) == len(set(ids)), "a user was listed more than once"
    assert page.total == 2


@pytest.mark.asyncio
async def test_no_service_filter_is_the_exact_complement(
    session: AsyncSession, mixed_users
) -> None:
    with_svc = await list_users(session, user_filter=UserFilter.WITH_SERVICE)
    without = await list_users(session, user_filter=UserFilter.NO_SERVICE)
    all_users = await list_users(session, user_filter=UserFilter.ALL)
    assert with_svc.total + without.total == all_users.total
    assert sorted(r.telegram_id for r in without.rows) == [101, 103, 104]


@pytest.mark.asyncio
async def test_the_total_counts_the_filter_not_the_whole_base(
    session: AsyncSession, mixed_users
) -> None:
    """«۱ نفر از ۵» on a filtered page would be two unrelated numbers."""
    page = await list_users(session, user_filter=UserFilter.WITH_BALANCE)
    assert page.total == 1
    assert page.has_next is False


@pytest.mark.asyncio
async def test_every_filter_has_a_label(session: AsyncSession) -> None:
    """A filter button with no label would render blank."""
    for value in UserFilter:
        assert USER_FILTER_LABELS.get(value), value


# --------------------------------------------------------------------------- #
# Operations on one customer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zeroing_a_wallet_goes_through_the_ledger(session: AsyncSession) -> None:
    """Money never moves without a row explaining it.

    Writing `wallet_balance_toman = 0` directly would be faster and would leave
    the owner unable to answer «این پول کجا رفت؟» a week later.
    """
    user = await _user(session, 200, username="tozero", balance=75_000)
    await session.commit()

    removed = await zero_wallet(session, user_id=user.id, admin_telegram_id=555)
    await session.commit()

    assert removed == 75_000
    await session.refresh(user)
    assert user.wallet_balance_toman == 0

    entries = (
        (
            await session.execute(
                select(WalletLedgerEntry).where(WalletLedgerEntry.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].entry_type is WalletEntryType.debit
    assert entries[0].amount_toman == 75_000
    assert entries[0].reason == MANUAL_DEBIT_REASON


@pytest.mark.asyncio
async def test_zeroing_an_empty_wallet_is_refused(session: AsyncSession) -> None:
    user = await _user(session, 201, username="empty")
    await session.commit()
    with pytest.raises(ManualAdjustError):
        await zero_wallet(session, user_id=user.id, admin_telegram_id=555)


@pytest.mark.asyncio
async def test_resetting_a_trial_keeps_the_history(session: AsyncSession) -> None:
    """Only the claim flag is cleared.

    The trial order and any panel account they still hold are what actually
    happened; deleting them to «undo» the trial would rewrite the record and
    could orphan a live account.
    """
    from datetime import UTC, datetime

    user = await _user(session, 202, username="trialer")
    user.trial_claimed_at = datetime.now(UTC)
    await _account(session, user, 50)
    await session.commit()

    await reset_trial(session, user.id)
    await session.commit()

    await session.refresh(user)
    assert user.trial_claimed_at is None
    accounts = (
        (
            await session.execute(
                select(PasarGuardAccount).where(PasarGuardAccount.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(accounts) == 1, "their existing service must survive the reset"


@pytest.mark.asyncio
async def test_resetting_a_trial_nobody_took_is_refused(session: AsyncSession) -> None:
    user = await _user(session, 203, username="never")
    await session.commit()
    with pytest.raises(ManualAdjustError):
        await reset_trial(session, user.id)


@pytest.mark.asyncio
async def test_the_card_numbers_reconcile(session: AsyncSession) -> None:
    """The old bot's card showed «پرداختی ۳۶۰٬۷۷۰» beside «خرید ۱۸۰٬۰۰۰» and a
    zero balance, which cannot all be true. Ours must add up."""
    user = await _user(session, 204, username="payer")
    await session.commit()
    await adjust_wallet_manually(
        session,
        user_id=user.id,
        amount_toman=300_000,
        admin_telegram_id=555,
        stamp="t1",
    )
    await session.commit()
    await adjust_wallet_manually(
        session,
        user_id=user.id,
        amount_toman=-120_000,
        admin_telegram_id=555,
        stamp="t2",
    )
    await session.commit()

    card = await get_customer_card(session, user.id)
    assert card.topped_up_toman == 300_000
    assert card.wallet_balance_toman == 180_000


@pytest.mark.asyncio
async def test_the_card_names_the_referrer_and_counts_invitees(
    session: AsyncSession, mixed_users
) -> None:
    card = await get_customer_card(session, mixed_users["referrer"].id)
    assert card.referral_count == 1
    assert card.referrer_label is None

    invited = await get_customer_card(session, mixed_users["invitee"].id)
    assert invited.referral_count == 0
    assert invited.referrer_label == "@referrer"
