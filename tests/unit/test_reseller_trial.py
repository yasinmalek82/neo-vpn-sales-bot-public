"""Reseller trials: free up to a monthly quota, charged after that.

A different operation from the customer trial, not a variation of it. A
customer takes one trial ever, for themselves; a reseller hands out many, one
per prospective customer who walks into the shop. So `trial_claimed_at` is
never touched here — that flag is the reseller's own personal trial, which they
may still take as a customer.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from api_client.exceptions import PasarGuardNotFoundError
from api_client.models import UserResponse, UserStatus
from database.base import Base
from database.models import ReportOutbox, User
from database.models.wallet import WalletLedgerEntry
from services.reseller import set_rate
from services.trial import (
    TrialError,
    get_reseller_trial_quota,
    issue_reseller_trial,
)
from services.wallet import credit


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def shop(session: AsyncSession) -> User:
    user = User(telegram_id=700, username="shop", referral_code="SHOP700", is_reseller=True)
    session.add(user)
    await session.flush()
    return user


# A real panel mints a fresh id per account; a fake that always says «1»
# collides on `pasarguard_accounts.panel_user_id` from the second trial on.
_next_panel_id = itertools.count(1)


def _panel_user(username: str) -> UserResponse:
    return UserResponse(
        id=next(_next_panel_id),
        username=username,
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=f"https://panel.example.com/sub/{username}",
        data_limit=2 * 1024**3,
    )


def _client() -> AsyncMock:
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=PasarGuardNotFoundError("missing", status_code=404))
    client.create_user = AsyncMock(side_effect=lambda payload: _panel_user(payload.username))
    client.subscription_url_from_response = lambda u: u.subscription_url
    return client


async def _issue(session: AsyncSession, shop: User, *, label: str | None = None):
    return await issue_reseller_trial(
        session,
        _client(),
        user_id=shop.id,
        data_limit_gb=2,
        duration_days=1,
        hwid_limit=1,
        group_ids=[1],
        display_name=label,
    )


# --------------------------------------------------------------------------- #
# The quota
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_with_no_rate_card_nothing_can_be_issued(session: AsyncSession, shop: User) -> None:
    """Quota zero and no price means there is no honest answer to «چند بدم؟»."""
    quota = await get_reseller_trial_quota(session, shop.id)
    assert quota.quota == 0
    assert quota.can_issue is False
    with pytest.raises(TrialError, match="quota_exhausted"):
        await _issue(session, shop)


@pytest.mark.asyncio
async def test_free_trials_up_to_the_quota(session: AsyncSession, shop: User) -> None:
    await set_rate(session, shop.id, trial_quota_monthly=3, trial_price_toman=5000)

    for expected_used in range(3):
        quota = await get_reseller_trial_quota(session, shop.id)
        assert quota.used == expected_used
        assert quota.is_free is True
        result = await _issue(session, shop)
        assert result.order.price_toman == 0

    await session.refresh(shop)
    assert shop.wallet_balance_toman == 0, "free trials must not touch the wallet"


@pytest.mark.asyncio
async def test_past_the_quota_the_wallet_pays(session: AsyncSession, shop: User) -> None:
    await set_rate(session, shop.id, trial_quota_monthly=1, trial_price_toman=5000)
    await credit(
        session,
        user_id=shop.id,
        amount_toman=20_000,
        reason="topup",
        idempotency_key="t1",
    )

    await _issue(session, shop)  # free
    await session.refresh(shop)
    assert shop.wallet_balance_toman == 20_000

    result = await _issue(session, shop)  # charged
    assert result.order.price_toman == 5000
    await session.refresh(shop)
    assert shop.wallet_balance_toman == 15_000

    entries = (
        (
            await session.execute(
                select(WalletLedgerEntry).where(WalletLedgerEntry.reason == "reseller_trial")
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_an_empty_wallet_past_the_quota_is_refused(session: AsyncSession, shop: User) -> None:
    await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=5000)
    with pytest.raises(TrialError, match="insufficient_funds"):
        await _issue(session, shop)


@pytest.mark.asyncio
async def test_last_months_trials_do_not_count(session: AsyncSession, shop: User) -> None:
    """The quota resets with the Jalali month the owner actually counts in."""
    await set_rate(session, shop.id, trial_quota_monthly=2, trial_price_toman=5000)
    await _issue(session, shop)
    await _issue(session, shop)
    assert (await get_reseller_trial_quota(session, shop.id)).remaining == 0

    # Same orders, counted from a month later.
    later = datetime.now(UTC) + timedelta(days=35)
    quota = await get_reseller_trial_quota(session, shop.id, now=later)
    assert quota.used == 0
    assert quota.remaining == 2


# --------------------------------------------------------------------------- #
# What a reseller trial must not do
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_it_does_not_burn_their_personal_trial(session: AsyncSession, shop: User) -> None:
    """They hand these out; their own customer trial is a separate thing."""
    await set_rate(session, shop.id, trial_quota_monthly=2)
    await _issue(session, shop)
    await session.refresh(shop)
    assert shop.trial_claimed_at is None


@pytest.mark.asyncio
async def test_an_ordinary_customer_cannot_use_this_path(session: AsyncSession) -> None:
    customer = User(telegram_id=701, referral_code="C701")
    session.add(customer)
    await session.flush()
    await set_rate(session, customer.id, trial_quota_monthly=5)

    with pytest.raises(TrialError, match="not_a_reseller"):
        await issue_reseller_trial(
            session,
            _client(),
            user_id=customer.id,
            data_limit_gb=2,
            duration_days=1,
            hwid_limit=1,
            group_ids=[1],
        )


@pytest.mark.asyncio
async def test_each_trial_gets_its_own_service_and_its_own_report(
    session: AsyncSession, shop: User
) -> None:
    """The outbox refuses a duplicate event key.

    Keyed on the telegram id — as the customer trial is — the second trial
    would be swallowed in silence and the owner would see one report for
    thirty giveaways.
    """
    await set_rate(session, shop.id, trial_quota_monthly=3)
    first = await _issue(session, shop, label="مشتری اول")
    second = await _issue(session, shop, label="مشتری دوم")

    assert first.account.id != second.account.id
    assert first.account.panel_username != second.account.panel_username
    assert (first.account.display_name, second.account.display_name) == (
        "مشتری اول",
        "مشتری دوم",
    )

    reports = (
        (await session.execute(select(ReportOutbox).where(ReportOutbox.event_key.like("trial:%"))))
        .scalars()
        .all()
    )
    assert len(reports) == 2, "each giveaway must report separately"


@pytest.mark.asyncio
async def test_charging_is_keyed_on_the_order(session: AsyncSession, shop: User) -> None:
    """A retry after a timeout must not debit twice for the same trial."""
    from services.wallet import debit

    await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=5000)
    await credit(
        session, user_id=shop.id, amount_toman=10_000, reason="topup", idempotency_key="t2"
    )
    result = await _issue(session, shop)

    before = shop.wallet_balance_toman
    await debit(
        session,
        user_id=shop.id,
        amount_toman=5000,
        reason="reseller_trial",
        idempotency_key=f"reseller_trial:{result.order.id}",
        order_id=result.order.id,
    )
    await session.refresh(shop)
    assert shop.wallet_balance_toman == before, "the repeat debit must be a no-op"


# --------------------------------------------------------------------------- #
# A giveaway is not a sale
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_free_trial_is_not_reported_as_a_sale(session: AsyncSession, shop: User) -> None:
    """Found by walking the feature, not by a test.

    Every provisioned order reported «🛒 فروش انجام شد», trials included — so a
    shop handing out three giveaways filled the owner's revenue feed with three
    zero-toman sales, on top of the three trial reports they already produced.
    """
    await set_rate(session, shop.id, trial_quota_monthly=2)
    await _issue(session, shop)

    reports = (await session.execute(select(ReportOutbox))).scalars().all()
    topics = [r.topic_key for r in reports]
    assert "sales" not in topics, "a giveaway must not appear in the sales feed"
    assert "customers" in topics, "but it is still a trial worth reporting"


@pytest.mark.asyncio
async def test_a_paid_reseller_trial_is_still_a_sale(session: AsyncSession, shop: User) -> None:
    """Money moved, so it belongs in the revenue feed. The rule is the price,
    not the word «trial»."""
    await set_rate(session, shop.id, trial_quota_monthly=0, trial_price_toman=5000)
    await credit(
        session, user_id=shop.id, amount_toman=10_000, reason="topup", idempotency_key="k9"
    )
    await _issue(session, shop)

    reports = (await session.execute(select(ReportOutbox))).scalars().all()
    assert "sales" in [r.topic_key for r in reports]


@pytest.mark.asyncio
async def test_the_finance_report_does_not_count_giveaways(
    session: AsyncSession, shop: User
) -> None:
    """«فروش امروز» is the number the owner steers by.

    Revenue was always right — a free trial adds zero toman — but the *count*
    included every handout, so three giveaways read as three sales.
    """
    from services.reporting import get_finance_report

    await set_rate(session, shop.id, trial_quota_monthly=2, trial_price_toman=5000)
    await credit(
        session, user_id=shop.id, amount_toman=10_000, reason="topup", idempotency_key="k10"
    )
    await _issue(session, shop)  # free
    await _issue(session, shop)  # free
    await _issue(session, shop)  # 5 000 toman
    await session.commit()

    report = await get_finance_report(session)
    assert report.today.orders == 1, "only the paid one is a sale"
    assert report.today.revenue_toman == 5000
