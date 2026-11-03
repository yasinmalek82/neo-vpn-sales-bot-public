"""Section ۷ب, part 1: the owner makes a reseller and sets what they pay.

Rows: ۷ب.۱ to ۷ب.۴, ۷ب.۱۱. ۷ب.۸ is STALE (the per-plan ↩️ button is gone from the
matrix; see the package report).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.texts import texts
from database.models import User
from tests.e2e._s07b_helpers import (
    matrix_prices,
    open_card,
    open_prices,
    set_plan_price,
    set_rates,
    tap_starting,
)
from tests.e2e.harness import App, Store, digits, plain, publish_store, smoke


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


async def _is_reseller(app: App, telegram_id: int) -> bool:
    user = await app.one(select(User).where(User.telegram_id == telegram_id))
    return bool(user.is_reseller)


@smoke("۷ب.۱")
async def test_granting_the_role_changes_the_card_and_adds_the_price_button(
    app: App, store: Store
) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    assert not any("قیمت‌های نمایندگی" in b for b in admin.buttons)
    assert plain(texts.admin_customer_role_customer) in admin.last_text
    await admin.tap("🏪 نماینده کردن")
    assert plain(texts.admin_customer_role_reseller) in admin.last_text
    assert any("قیمت‌های نمایندگی" in b for b in admin.buttons), admin.buttons
    assert await _is_reseller(app, rez.user_id)


@smoke("۷ب.۲", "۷ب.۲الف", "۷ب.۲ب", "۷ب.۲ج", "۷ب.۳")
async def test_the_price_panel_is_three_sections_each_with_only_its_own_settings(
    app: App, store: Store
) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await admin.tap("🏪 نماینده کردن")
    await open_prices(admin)

    # ۷ب.۲: three separate sections, not one crowded page.
    sections = admin.rows[:3]
    assert [len(r) for r in sections] == [1, 1, 1]
    assert sections[0][0].startswith("🧩") and sections[1][0].startswith("🧪")
    assert sections[2][0].startswith("🎁")
    assert not any("هر گیگ" in b or "سهمیه" in b for b in admin.buttons)

    # ۷ب.۲الف: only the two rates, with the formula written out.
    await admin.tap("🧩")
    assert plain(texts.admin_reseller_custom_intro) == admin.last_text
    assert [b for b in admin.buttons if ":" in b] == ["هر گیگ: ۰", "هر روز: ۰"]
    # ۷ب.۳: the numbers the owner types are the numbers on the buttons.
    await admin.tap("⬅️ قیمت")
    await set_rates(admin, 1200, 500)
    await admin.tap("🧩")
    assert [b for b in admin.buttons if ":" in b] == ["هر گیگ: ۱٬۲۰۰", "هر روز: ۵۰۰"]
    await admin.tap("⬅️ قیمت")

    # ۷ب.۲ب: only the monthly quota and the price of an extra trial.
    await admin.tap("🧪")
    assert plain(texts.admin_reseller_trial_intro) == admin.last_text
    assert [b for b in admin.buttons if ":" in b] == ["سهمیهٔ ماهانه: ۰", "هر تست اضافه: ۰"]
    await admin.tap("⬅️ قیمت")

    # ۷ب.۲ج: plans sit under their group's name, so the two «۵۰ گیگ» differ.
    await admin.tap("🎁")
    assert [b.removesuffix(" ✅") for b in admin.rows[0]] == ["نامحدود", "ویژه", "اقتصادی"]
    await tap_starting(admin, "ویژه")
    special = [b for b in admin.buttons if b.startswith("۵۰ گیگ / ۳۰ روز")]
    await tap_starting(admin, "اقتصادی")
    economy = [b for b in admin.buttons if b.startswith("۵۰ گیگ / ۳۰ روز")]
    assert len(special) == len(economy) == 1
    assert "150000" in digits(special[0]) and "90000" in digits(economy[0])


@smoke("۷ب.۲د", "۷ب.۴")
async def test_a_price_lands_on_the_button_and_the_owner_stays_in_the_section(
    app: App, store: Store
) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await admin.tap("🏪 نماینده کردن")
    await open_prices(admin)
    await set_plan_price(admin, "ویژه", "۵۰ گیگ / ۳۰ روز", 100_000)

    # Back on the same group tab, with the price on the plan's own button.
    assert plain(texts.admin_reseller_matrix_title.format(name="rez")) == admin.last_text
    assert admin.rows[0] == ["نامحدود", "ویژه ✅", "اقتصادی"]
    plan = next(b for b in admin.buttons if b.startswith("۵۰ گیگ / ۳۰ روز"))
    assert matrix_prices(plan) == (150_000, 100_000), plan

    # A second and third price in a row never leave the section.
    await tap_starting(admin, "اقتصادی")
    await tap_starting(admin, "۵۰ گیگ / ۳۰ روز")
    await admin.send("70000")
    assert admin.rows[0] == ["نامحدود", "ویژه", "اقتصادی ✅"], admin.rows[0]
    economy = next(b for b in admin.buttons if b.startswith("۵۰ گیگ / ۳۰ روز"))
    assert matrix_prices(economy) == (90_000, 70_000), economy
    await tap_starting(admin, "نامحدود")
    await tap_starting(admin, "۱ کاربره")
    await admin.send("200000")
    unlimited = next(b for b in admin.buttons if b.startswith("۱ کاربره"))
    assert matrix_prices(unlimited) == (250_000, 200_000), unlimited
    assert plain(texts.admin_reseller_panel.format(name="rez")) != admin.last_text


@smoke("۷ب.۲د")
async def test_typing_a_plan_price_causes_no_rejected_telegram_call(app: App, store: Store) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await admin.tap("🏪 نماینده کردن")
    await open_prices(admin)
    await admin.tap("🎁")
    await tap_starting(admin, "ویژه")
    await tap_starting(admin, "۵۰ گیگ / ۳۰ روز")
    await admin.send("100000")
    assert app.tg.unaccepted() == []


@smoke("۷ب.۱۱")
async def test_revoking_and_granting_again_keeps_the_prices(app: App, store: Store) -> None:
    rez = app.customer("rez")
    await rez.start()
    admin = app.admin()
    await open_card(admin, "rez")
    await admin.tap("🏪 نماینده کردن")
    await open_prices(admin)
    await set_rates(admin, 1200, 500)
    await set_plan_price(admin, "ویژه", "۵۰ گیگ / ۳۰ روز", 100_000)
    await admin.tap("⬅️ قیمت")
    await admin.tap("⬅️ کارت")

    await admin.tap("حذف نمایندگی")
    await admin.tap("⛔️ بله، حذف کن")
    assert not await _is_reseller(app, rez.user_id)
    assert not any("قیمت‌های نمایندگی" in b for b in admin.buttons)
    await admin.tap("🏪 نماینده کردن")
    assert await _is_reseller(app, rez.user_id)

    await open_prices(admin)
    assert any("(۱ از ۵)" in b for b in admin.buttons), admin.buttons
    await admin.tap("🧩")
    assert [b for b in admin.buttons if ":" in b] == ["هر گیگ: ۱٬۲۰۰", "هر روز: ۵۰۰"]
    await admin.tap("⬅️ قیمت")
    await admin.tap("🎁")
    await tap_starting(admin, "ویژه")
    plan = next(b for b in admin.buttons if b.startswith("۵۰ گیگ / ۳۰ روز"))
    assert matrix_prices(plan) == (150_000, 100_000), plan
