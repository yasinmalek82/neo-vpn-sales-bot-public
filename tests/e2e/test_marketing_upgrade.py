"""Section ۱۷, rows ۱۷.۶ to ۱۷.۸: the upgrade wording of the volume warning.

It is a second wording of the existing warning, under the same event key, so a
customer never gets both. The standard warning's own tests are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select, update

from bot.texts import texts
from database.models import Order, User
from database.models.lifecycle import ServiceNotice
from services.store_admin import commit_and_activate, create_draft_package
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s17_helpers import switch_on
from tests.e2e.harness import GB, Actor, App, Store, plain, publish_store, smoke
from tests.e2e.harness.panel import PanelUser


@pytest_asyncio.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _service(app: App, store: Store, plan_key: str = "special_50") -> tuple[Actor, PanelUser]:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    return ali, await buy_active(app, store, ali, plan_key)


async def _warn(app: App, who: Actor) -> list[str]:
    mark = who.mark()
    await app.run_job("volume")
    return [m for m in who.texts_since(mark) if "داره تموم می‌شه" in m]


@smoke("۱۷.۶")
async def test_a_service_burning_fast_gets_the_upgrade_wording_once(app: App, store: Store) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade")
    panel_user.used_traffic = int(45 * GB)  # 10% of 50 GB left, a fresh 30 day period

    got = await _warn(app, ali)
    assert len(got) == 1
    assert "«100 گیگ / 90 روز» بهت بیشتر می‌خوره" in got[0], got
    assert "10٪" in got[0] and "29 روز" in got[0], got
    assert "زودتر از زمانش" in got[0], got
    assert "تغییر پلن" in ali.buttons and "تمدید همین سرویس" in ali.buttons
    assert await _warn(app, ali) == [], "the same key: not again, and not the standard one either"
    async with app.session() as session:
        keys = (await session.scalars(select(ServiceNotice.event_key))).all()
    assert len(keys) == 1 and keys[0].startswith("volwarn:")


@smoke("۱۷.۶")
async def test_the_plan_switch_button_opens_the_switch_for_this_service(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade")
    panel_user.used_traffic = int(45 * GB)
    assert len(await _warn(app, ali)) == 1
    await ali.tap("تغییر پلن")
    assert plain(texts.purchase_switch_pick) in ali.last_text


@smoke("۱۷.۶")
async def test_the_smallest_bigger_package_of_the_same_group_is_the_suggestion(
    app: App, store: Store
) -> None:
    async with app.session() as session:
        await create_draft_package(
            session,
            category_id=store.category_ids["special"],
            title_fa="۷۵ گیگ / ۳۰ روز",
            fixed_price_toman=200_000,
            volume_gb=75,
            duration_days=30,
        )
        await create_draft_package(
            session,
            category_id=store.category_ids["economy"],
            title_fa="۶۰ گیگ اقتصادی",
            fixed_price_toman=100_000,
            volume_gb=60,
            duration_days=30,
        )
        await commit_and_activate(session, notes="پلن بیشتر")
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade")
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "«75 گیگ / 30 روز»" in got[0], got


@smoke("۱۷.۷")
async def test_with_the_campaign_off_the_warning_is_the_standard_one(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got
    assert "📅 تمدید این سرویس" in ali.buttons and "تغییر پلن" not in ali.buttons


@smoke("۱۷.۷")
async def test_a_campaign_switched_on_later_does_not_touch_a_warning_due_now(
    app: App, store: Store
) -> None:
    await switch_on(app, "upgrade", upgrade_enabled_at=datetime.now(UTC) + timedelta(hours=1))
    ali, panel_user = await _service(app, store)
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got


@smoke("۱۷.۷")
async def test_a_slow_burn_is_not_fast(app: App, store: Store) -> None:
    await switch_on(app, "upgrade")
    # Slow: 25 of 30 days used, so 17% of the time is left.
    ali, panel_user = await _service(app, store)
    async with app.session() as session:
        await session.execute(
            update(Order).values(activated_at=datetime.now(UTC) - timedelta(days=25))
        )
        await session.commit()
    panel_user.expire = datetime.now(UTC) + timedelta(days=5)
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got


@smoke("۱۷.۷")
async def test_no_start_date_is_not_fast(app: App, store: Store) -> None:
    await switch_on(app, "upgrade")
    ali, panel_user = await _service(app, store)
    async with app.session() as session:
        await session.execute(update(Order).values(activated_at=None))
        await session.commit()
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got


@smoke("۱۷.۷")
async def test_a_group_with_nothing_bigger_has_nothing_to_suggest(app: App, store: Store) -> None:
    """The economy group sells one 50 GB package only."""
    await switch_on(app, "upgrade")
    ali, panel_user = await _service(app, store, "economy_50")
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got


@smoke("۱۷.۸")
async def test_a_reseller_keeps_the_standard_wording(app: App, store: Store) -> None:
    await switch_on(app, "upgrade")
    ali, panel_user = await _service(app, store)
    async with app.session() as session:
        await session.execute(update(User).values(is_reseller=True))
        await session.commit()
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got


@smoke("۱۷.۸")
async def test_a_walk_in_customers_service_keeps_the_standard_wording(
    app: App, store: Store
) -> None:
    """A former reseller: the flag is gone, the label stays on the order."""
    await switch_on(app, "upgrade")
    ali, panel_user = await _service(app, store)
    async with app.session() as session:
        await session.execute(update(Order).values(customer_label="رضا"))
        await session.commit()
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" not in got[0], got
