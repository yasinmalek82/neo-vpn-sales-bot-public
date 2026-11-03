"""Section ۱۷, rows ۱۷.۱۴ to ۱۷.۱۹: the personal offer, from the owner's screen to the till."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from bot.keyboards.marketing import CODE_OF_CAMPAIGN, MktCB
from bot.texts import texts
from database.models import Order, User, UserOffer
from services.ux_format import to_persian_digits
from tests.e2e._s08_helpers import buy_active
from tests.e2e._s17_helpers import marketing_row, open_marketing, switch_on
from tests.e2e.harness import GB, Actor, App, Store, browse_to_plan, publish_store, smoke
from tests.e2e.harness.expectations import report_plain
from tests.e2e.harness.panel import PanelUser

OFFER_CAMPAIGNS = ("trial_convert", "winback", "upgrade")


def _percent_cb(campaign: str) -> str:
    return f"{MktCB.PERCENT}{CODE_OF_CAMPAIGN[campaign]}"


def _hours_cb(campaign: str) -> str:
    return f"{MktCB.HOURS}{CODE_OF_CAMPAIGN[campaign]}"


def _has_keyboard(admin: Actor) -> bool:
    return admin.last_message.markup is not None and bool(admin.last_message.rows)


def _percent_buttons(admin: Actor) -> list[str]:
    return [b for b in admin.buttons if "تخفیف" in b and "مهلت" not in b]


def _hours_buttons(admin: Actor) -> list[str]:
    return [b for b in admin.buttons if "مهلت تخفیف" in b]


# --- the owner's screen --------------------------------------------------------


@smoke("۱۷.۱۴")
async def test_three_campaigns_have_a_percent_button_and_the_cart_has_none(app: App) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    assert len(_percent_buttons(admin)) == 3, admin.buttons
    assert all(b.startswith("❌") for b in _percent_buttons(admin)), "0 reads as off"
    assert _hours_buttons(admin) == [], "hours matter only when a percent is set"
    assert "تخفیفی توی این پیام‌ها نیست" not in admin.last_text, "the screen must not deny it"
    assert (await marketing_row(app)) is None, "looking at the screen writes nothing"

    # A forged tap for the cart is refused, with a keyboard under the answer.
    await admin.press(_percent_cb("cart"))
    assert admin.popup == texts.admin_action_error
    assert (await marketing_row(app)) is None


@smoke("۱۷.۱۴")
async def test_a_typed_percent_is_saved_and_shown_on_its_button(app: App) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    await admin.press(_percent_cb("winback"))
    assert "50" in admin.last_text and _has_keyboard(admin)
    assert "❌ لغو" in admin.buttons
    await admin.send("۲۵")
    assert texts.admin_mkt_saved in admin.last_text
    assert any("۲۵٪" in b and b.startswith("✅") for b in _percent_buttons(admin)), admin.buttons
    assert len(_hours_buttons(admin)) == 1, "the hours control appears with a percent"
    row = await marketing_row(app)
    assert row is not None and row.winback_offer_percent == 25
    assert row.trial_convert_offer_percent == 0 and row.upgrade_offer_percent == 0
    assert row.winback_enabled_at is None, "setting a percent does not switch the campaign on"
    assert _has_keyboard(admin)


@smoke("۱۷.۱۴")
async def test_a_percent_outside_zero_to_fifty_is_refused_and_the_next_try_is_read(
    app: App,
) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    await admin.press(_percent_cb("trial_convert"))
    for bad in ("51", "-1", "ده", "۱۰.۵"):
        await admin.send(bad)
        assert admin.last_text.startswith("❌"), (bad, admin.last_text)
        assert "50" in admin.last_text and _has_keyboard(admin)
        assert (await marketing_row(app)) is None, "a refused number stores nothing"
    await admin.send("10")
    row = await marketing_row(app)
    assert row is not None and row.trial_convert_offer_percent == 10


@smoke("۱۷.۱۴")
async def test_hours_are_one_to_one_sixty_eight_and_zero_percent_hides_them_again(
    app: App,
) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    await admin.press(_percent_cb("upgrade"))
    await admin.send("20")
    await admin.press(_hours_cb("upgrade"))
    assert "168" in admin.last_text and _has_keyboard(admin)
    for bad in ("0", "169", "x"):
        await admin.send(bad)
        assert admin.last_text.startswith("❌"), (bad, admin.last_text)
        assert "168" in admin.last_text and _has_keyboard(admin)
    row = await marketing_row(app)
    assert row is not None and row.upgrade_offer_hours == 48, "refused values store nothing"
    await admin.send("۷۲")
    assert any("۷۲" in b for b in _hours_buttons(admin)), admin.buttons
    row = await marketing_row(app)
    assert row is not None and row.upgrade_offer_hours == 72 and row.upgrade_offer_percent == 20

    await admin.press(_percent_cb("upgrade"))
    await admin.send("0")
    assert _hours_buttons(admin) == [], "nothing to time out once the percent is 0"


@smoke("۱۷.۱۴")
async def test_cancel_leaves_the_prompt_and_a_helper_admin_cannot_set_anything(app: App) -> None:
    admin = app.admin(0)
    await open_marketing(admin)
    await admin.press(_percent_cb("winback"))
    await admin.tap("❌ لغو")
    await admin.send("30")
    assert (await marketing_row(app)) is None, "out of the state a number is not read"

    helper = app.admin(1)
    await open_marketing(helper)
    await helper.press(_percent_cb("winback"))
    assert helper.popup == texts.admin_mkt_not_owner
    await helper.press(_hours_cb("winback"))
    assert helper.popup == texts.admin_mkt_not_owner
    assert (await marketing_row(app)) is None


# --- the upgrade message carries the offer ---------------------------------------


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


@smoke("۱۷.۱۵")
async def test_the_upgrade_wording_with_a_percent_writes_an_offer_for_a_bigger_package(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(45 * GB)

    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" in got[0], got
    assert _line(24, 20) in got[0], got

    rows = await _offers(app)
    assert len(rows) == 1
    offer = rows[0]
    assert offer.campaign == "upgrade" and offer.percent == 20
    assert offer.min_volume_gb == 50, "the service's own limit: only a bigger package qualifies"
    assert offer.event_key.startswith("volwarn:") and offer.order_id is None
    expires = offer.expires_at if offer.expires_at.tzinfo else offer.expires_at.replace(tzinfo=UTC)
    assert abs(expires - (datetime.now(UTC) + timedelta(hours=24))) < timedelta(minutes=5)

    assert await _warn(app, ali) == [], "not again, and no second offer row"
    assert len(await _offers(app)) == 1


@smoke("۱۷.۱۵")
async def test_with_percent_zero_the_upgrade_wording_has_no_line_and_no_row(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade")
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "زودتر از زمانش" in got[0], got
    assert "تخفیف" not in got[0]
    assert await _offers(app) == []


@smoke("۱۷.۱۵")
async def test_the_standard_wording_never_carries_an_offer(app: App, store: Store) -> None:
    ali, panel_user = await _service(app, store)
    # The campaign is off, so this is the plain warning, whatever percent is stored.
    await switch_on(app, upgrade_offer_percent=20)
    panel_user.used_traffic = int(45 * GB)
    got = await _warn(app, ali)
    assert len(got) == 1 and "تخفیف" not in got[0], got
    assert await _offers(app) == []


@smoke("۱۷.۱۵")
async def test_an_upgrade_message_the_customer_blocked_has_its_offer_voided(
    app: App, store: Store
) -> None:
    ali, panel_user = await _service(app, store)
    await switch_on(app, "upgrade", upgrade_offer_percent=20, upgrade_offer_hours=24)
    panel_user.used_traffic = int(45 * GB)
    app.tg.block(ali.user_id)
    await app.run_job("volume")
    rows = await _offers(app)
    assert len(rows) == 1 and rows[0].voided_at is not None


# --- the customer's own path -------------------------------------------------------


@smoke("۱۷.۱۶")
async def test_a_customer_with_an_offer_sees_it_in_the_summary_and_pays_that_price(
    app: App, store: Store
) -> None:
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    async with app.session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == ali.user_id))
        ).scalar_one()
        offer = UserOffer(
            user_id=user.id,
            campaign="winback",
            percent=10,
            event_key="e2e:offer:1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(offer)
        await session.commit()
        offer_id = int(offer.id)

    await browse_to_plan(ali, store, "special_50")
    await ali.send("ali12")
    await ali.tap("بدون کد تخفیف")
    line = texts.purchase_personal_offer_line.format(percent=to_persian_digits(10))
    assert report_plain(line) in ali.last_text, ali.last_text
    await ali.tap("ثبت سفارش")

    order = await app.latest_order(ali)
    original = order.price_toman + order.discount_toman
    assert order.discount_toman == (original * 10 + 50) // 100
    async with app.session() as session:
        stored = await session.get(UserOffer, offer_id)
        assert stored is not None and stored.order_id == order.id
        assert (await session.scalar(select(Order.id).where(Order.id == order.id))) is not None
