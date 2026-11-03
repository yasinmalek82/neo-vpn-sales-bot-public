"""A shop's customer list: paged, searchable, labelled by *their* customer.

Why this is not the customer screen with a filter bolted on: the two answer
different questions. A customer asks «سرویس خودم چطوره؟» and owns two. A shop
asks «سرویس آقای رضایی کِی تموم می‌شه؟» and holds thirty, none of them their own.

Paging is also a cost control, not only a layout choice — the list screen asks
the panel for live status once per row, so a page is a batch of HTTP calls.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from database.models.pasarguard_account import PasarGuardAccount
from services.accounts import RESELLER_PAGE_SIZE, list_services_page


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
    user = User(telegram_id=800, referral_code="SHOP800", is_reseller=True)
    session.add(user)
    await session.flush()
    return user


async def _service(session: AsyncSession, user: User, n: int, label: str | None) -> None:
    session.add(
        PasarGuardAccount(
            user_id=user.id,
            panel_user_id=5000 + n,
            panel_username=f"neo_{n:03d}",
            subscription_url=f"https://panel.example.com/sub/neo_{n:03d}",
            display_name=label,
        )
    )


@pytest.mark.asyncio
async def test_an_empty_shop_pages_without_blowing_up(session: AsyncSession, shop: User) -> None:
    page = await list_services_page(session, shop.id)
    assert page.rows == []
    assert page.total == 0
    assert page.has_next is False


@pytest.mark.asyncio
async def test_paging_walks_every_service_exactly_once(session: AsyncSession, shop: User) -> None:
    for i in range(25):
        await _service(session, shop, i, f"مشتری {i}")
    await session.commit()

    seen: list[str] = []
    offset = 0
    for _ in range(10):
        page = await list_services_page(session, shop.id, offset=offset)
        assert len(page.rows) <= RESELLER_PAGE_SIZE
        seen.extend(r.panel_username for r in page.rows)
        if not page.has_next:
            break
        offset = page.next_offset

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a service appeared on two pages"


@pytest.mark.asyncio
async def test_prev_steps_back_a_full_page(session: AsyncSession, shop: User) -> None:
    for i in range(25):
        await _service(session, shop, i, f"مشتری {i}")
    await session.commit()

    last = await list_services_page(session, shop.id, offset=20)
    assert len(last.rows) == 5
    assert last.prev_offset == 10, "a partial last page must not shrink the step"


@pytest.mark.asyncio
async def test_search_finds_a_customer_by_the_label_the_shop_typed(
    session: AsyncSession, shop: User
) -> None:
    """The only handle a shop has. Nobody remembers which `neo_007` is whose."""
    await _service(session, shop, 1, "رضایی ۰۹۱۲۳۴۵")
    await _service(session, shop, 2, "کریمی")
    await _service(session, shop, 3, "رضوی")
    await session.commit()

    page = await list_services_page(session, shop.id, query="رضایی")
    assert [r.display_name for r in page.rows] == ["رضایی ۰۹۱۲۳۴۵"]
    assert page.total == 1
    assert page.query == "رضایی"


@pytest.mark.asyncio
async def test_search_matches_a_phone_number_inside_the_label(
    session: AsyncSession, shop: User
) -> None:
    await _service(session, shop, 1, "رضایی ۰۹۱۲۳۴۵")
    await session.commit()
    page = await list_services_page(session, shop.id, query="۰۹۱۲۳۴۵")
    assert len(page.rows) == 1


@pytest.mark.asyncio
async def test_search_also_matches_the_panel_username(session: AsyncSession, shop: User) -> None:
    """When the shop is reading a username off the panel, not their own label."""
    await _service(session, shop, 7, "کریمی")
    await session.commit()
    page = await list_services_page(session, shop.id, query="neo_007")
    assert len(page.rows) == 1


@pytest.mark.asyncio
async def test_a_search_with_no_hits_is_empty_not_everything(
    session: AsyncSession, shop: User
) -> None:
    """A filter that silently falls back to «all» is worse than one that says no."""
    await _service(session, shop, 1, "کریمی")
    await session.commit()
    page = await list_services_page(session, shop.id, query="هیچ‌کس")
    assert page.rows == []
    assert page.total == 0


@pytest.mark.asyncio
async def test_search_pages_too(session: AsyncSession, shop: User) -> None:
    """Twelve customers of one family must not lose two of them off the page."""
    for i in range(12):
        await _service(session, shop, i, f"رضایی {i}")
    await _service(session, shop, 99, "کریمی")
    await session.commit()

    first = await list_services_page(session, shop.id, query="رضایی")
    assert first.total == 12
    assert len(first.rows) == RESELLER_PAGE_SIZE
    assert first.has_next is True

    second = await list_services_page(session, shop.id, offset=first.next_offset, query="رضایی")
    assert len(second.rows) == 2
    assert second.has_next is False


@pytest.mark.asyncio
async def test_unlabelled_services_stay_out_of_the_list(session: AsyncSession, shop: User) -> None:
    """Same «named only» rule the customer screen uses — the two must agree."""
    await _service(session, shop, 1, "کریمی")
    await _service(session, shop, 2, None)
    await session.commit()

    page = await list_services_page(session, shop.id)
    assert [r.display_name for r in page.rows] == ["کریمی"]


@pytest.mark.asyncio
async def test_one_shop_never_sees_another_shops_customers(
    session: AsyncSession, shop: User
) -> None:
    other = User(telegram_id=801, referral_code="SHOP801", is_reseller=True)
    session.add(other)
    await session.flush()
    await _service(session, shop, 1, "مال من")
    await _service(session, other, 2, "مال اون")
    await session.commit()

    page = await list_services_page(session, shop.id)
    assert [r.display_name for r in page.rows] == ["مال من"]
    assert page.total == 1
