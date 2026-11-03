"""Section ۱۷, row ۱۷.۱۵ (second audit): who is promised the upgrade offer.

* An open order: the warning and its upgrade wording still go out, the offer does not.
* One upgrade offer per service per period, however many thresholds the period crosses.
* An offer is live only while its message is out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select, update

from bot.texts import texts
from database.models import Order, OrderStatus, User, UserOffer
from database.models.lifecycle import ServiceNotice
from services.user_offers import best_live_offer
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s17_helpers import switch_on
from tests.e2e.harness import GB, Actor, App, Store, publish_store, smoke
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.panel import PanelUser


@pytest_asyncio.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _offers(app: App) -> list[UserOffer]:
    async with app.session() as session:
        return list((await session.scalars(select(UserOffer).order_by(UserOffer.id))).all())


async def _warn(app: App, who: Actor) -> list[str]:
    mark = who.mark()
    await app.run_job("volume")
    return [m for m in who.texts_since(mark) if "داره تموم می‌شه" in m]


def _line(hours: int, percent: int) -> str:
    return report_plain(texts.notify_offer_line.format(hours=str(hours), percent=str(percent)))


async def _service(app: App, store: Store) -> tuple[Actor, PanelUser]:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    return ali, await buy_active(app, store, ali, "special_50")


async def _hold_an_open_order(app: App, who: Actor) -> None:
    async with app.session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == who.user_id))
        ).scalar_one()
        plan_id = await session.scalar(select(Order.plan_id).where(Order.user_id == user.id))
        session.add(
            Order(
                user_id=user.id,
                plan_id=plan_id,
                status=OrderStatus.PENDING_PAYMENT,
                price_toman=100_000,
            )
        )
        await session.commit()


@smoke("۱۷.۱۵ب")
async def test_an_open_order_gets_the_upgrade_wording_but_no_offer(app: App, store: Store) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    await _hold_an_open_order(app, ali)
    panel_user.used_traffic = int(45 * GB)

    got = await _warn(app, ali)
    assert len(got) == 1, "the warning itself is never held back"
    assert "زودتر از زمانش" in got[0] and "تخفیف" not in got[0], got
    assert await _offers(app) == []


@smoke("۱۷.۱۵ب")
async def test_a_second_threshold_of_the_same_period_gets_the_wording_but_no_second_offer(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)

    panel_user.used_traffic = int(40 * GB)  # 20% left: the 20 threshold
    first = await _warn(app, ali)
    assert len(first) == 1 and _line(24, 20) in first[0], first
    assert len(await _offers(app)) == 1

    panel_user.used_traffic = int(48 * GB)  # 4% left: the 5 threshold, same limit and expiry
    second = await _warn(app, ali)
    assert len(second) == 1 and "زودتر از زمانش" in second[0], second
    assert "تخفیف" not in second[0], second
    assert len(await _offers(app)) == 1, "one offer per service per period"


@smoke("۱۷.۱۵ب")
async def test_a_new_period_gets_a_new_offer(app: App, store: Store) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(40 * GB)
    assert _line(24, 20) in (await _warn(app, ali))[0]

    panel_user.data_limit = int(60 * GB)  # topped up: a new (limit, expiry) period
    panel_user.used_traffic = int(50 * GB)  # 16.7% left
    again = await _warn(app, ali)
    assert len(again) == 1 and _line(24, 20) in again[0], again
    rows = await _offers(app)
    assert [r.min_volume_gb for r in rows] == [50, 60]


@smoke("۱۷.۱۵ب")
async def test_a_spent_offer_of_the_period_still_blocks_a_second_one(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(40 * GB)
    assert len(await _warn(app, ali)) == 1
    async with app.session() as session:
        offer = (await session.scalars(select(UserOffer))).one()
        offer.expires_at = datetime.now(UTC) - timedelta(hours=1)  # lapsed, never used
        await session.commit()

    panel_user.used_traffic = int(48 * GB)
    second = await _warn(app, ali)
    assert len(second) == 1 and "تخفیف" not in second[0], second
    assert len(await _offers(app)) == 1


@smoke("۱۷.۱۵ب")
async def test_a_warning_sent_again_after_a_reset_does_not_promise_an_offer_an_order_took(
    app: App, store: Store
) -> None:
    """A periodic reset keeps the limit and the expiry, so the same warning key comes back."""
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(40 * GB)
    assert _line(24, 20) in (await _warn(app, ali))[0]
    async with app.session() as session:
        offer = (await session.scalars(select(UserOffer))).one()
        offer.order_id = await session.scalar(
            select(Order.id).where(Order.status == OrderStatus.ACTIVE)
        )
        assert offer.order_id is not None
        await session.commit()
    spent = (await _offers(app))[0]

    panel_user.used_traffic = 0  # the reset: no warning is due, the old one is forgotten
    assert await _warn(app, ali) == []
    panel_user.used_traffic = int(40 * GB)
    again = await _warn(app, ali)

    assert len(again) == 1 and "زودتر از زمانش" in again[0], again
    assert "تخفیف" not in again[0], "the offer was spent: the message must not promise it"
    rows = await _offers(app)
    assert len(rows) == 1 and rows[0].order_id == spent.order_id
    assert rows[0].expires_at == spent.expires_at, "a spent row is left alone"


@smoke("۱۷.۱۵ب")
async def test_a_retry_of_the_same_warning_keeps_its_row_and_its_line(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(40 * GB)
    app.tg.flood_next("SendMessage")  # a transient failure: the claim is put back

    assert await _warn(app, ali) == []
    failed = await _offers(app)
    assert len(failed) == 1 and failed[0].voided_at is not None, "a message nobody saw"
    async with app.session() as session:
        assert (
            await best_live_offer(session, user_id=failed[0].user_id, volume_gb=100) is None
        ), "not live while its message is not out"

    got = await _warn(app, ali)
    assert len(got) == 1 and _line(24, 20) in got[0], got
    rows = await _offers(app)
    assert len(rows) == 1 and rows[0].id == failed[0].id, "the same row, not a second one"
    assert rows[0].voided_at is None
    assert rows[0].expires_at > failed[0].expires_at, "moved to the new send"
    async with app.session() as session:
        live = await best_live_offer(session, user_id=rows[0].user_id, volume_gb=100)
    assert live is not None and live.id == rows[0].id


@smoke("۱۷.۱۵ب")
async def test_a_claim_that_crashed_before_its_send_is_retried_with_its_own_row_and_line(
    app: App, store: Store
) -> None:
    """The offer row was written with the claim and never voided (no send failure was seen).
    The same key claimed again must find its own row, not count it as «another threshold»."""
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(40 * GB)
    assert _line(24, 20) in (await _warn(app, ali))[0]
    first = await _offers(app)
    assert len(first) == 1 and first[0].voided_at is None

    async with app.session() as session:  # as if the process died between claim and send
        await session.execute(
            update(ServiceNotice).values(
                delivered_at=None, sent_at=datetime.now(UTC) - timedelta(minutes=11)
            )
        )
        await session.commit()

    again = await _warn(app, ali)
    assert len(again) == 1 and _line(24, 20) in again[0], again
    rows = await _offers(app)
    assert len(rows) == 1 and rows[0].id == first[0].id
