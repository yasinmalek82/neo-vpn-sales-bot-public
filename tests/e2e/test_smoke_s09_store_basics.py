"""Section ۹, part 1: the owner builds a group and a plan and the customer sees it at once.

Rows ۹.۱ to ۹.۱۱.
STALE: ۹.۱ names «🛍 مدیریت فروشگاه» and «پیش‌نویس / انتشار»: the button is «🛍 فروشگاه» and
the screen is «مدیریت فروشگاه»; there is still no draft or publish button, which is the point.
۹.۳ asks «حجم → مدت → قیمت»: the wizard now also asks the device limit («۳ از ۴»).
"""

from __future__ import annotations

from bot.keyboards.store_admin import StoreCB
from bot.texts import texts
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s09_helpers import (
    all_plans,
    drafts,
    live_catalog,
    live_plans,
    make_group,
    new_plan,
    open_store,
    set_panel_group,
    shelf,
)
from tests.e2e.harness import App, digits, plain, publish_store, smoke


@smoke("۹.۱")
async def test_the_store_screen_has_no_draft_or_publish_button(app: App) -> None:
    await publish_store(app)
    admin = app.admin()
    await open_store(admin)
    assert "مدیریت فروشگاه" in admin.last_text
    everything = " ".join(admin.buttons) + admin.last_text
    assert "پیش‌نویس" not in everything and "انتشار" not in everything, admin.buttons
    # And not one level down either.
    await tap_starting(admin, "حجمی")
    await tap_starting(admin, "ویژه")
    everything = " ".join(admin.buttons) + admin.last_text
    assert "پیش‌نویس" not in everything and "انتشار" not in everything, admin.buttons
    assert await drafts(app) == 0, "merely looking at the store minted a draft"


@smoke("۹.۲")
async def test_a_new_group_is_made_once_and_not_duplicated(app: App) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    assert "ساخته شد" in admin.last_text and "📂 پریمیوم" in admin.last_text
    await tap_starting(admin, "⬅️ بازگشت به فروشگاه")
    assert "1 گروه" in admin.last_text
    view = await live_catalog(app)
    titles = [c.title_fa for c in view.categories]
    assert titles.count("پریمیوم") == 1, titles


@smoke("۹.۳")
async def test_the_plan_wizard_asks_volume_duration_and_price_by_buttons_and_never_sticks(
    app: App,
) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await tap_starting(admin, "➕ پلن جدید")
    assert "حجم را انتخاب کن" in admin.last_text
    assert {"۱۰ گیگ", "۵۰ گیگ", "۱۰۰ گیگ"} <= set(admin.buttons)
    await tap_starting(admin, "۵۰ گیگ")
    assert "برای چه مدت" in admin.last_text
    assert {"۳۰ روز", "۹۰ روز", "♾ بدون انقضا"} <= set(admin.buttons), admin.buttons
    await tap_starting(admin, "۹۰ روز")  # the step the owner's note says used to die
    assert "کاربر همزمان" in admin.last_text
    await tap_starting(admin, "۲")
    assert "قیمت" in admin.last_text and "تومان" in admin.last_text
    await admin.send("390000")
    assert "50 گیگ" in admin.last_text and "90 روز" in admin.last_text
    assert "390,000 تومان" in admin.last_text
    assert await all_plans(app) == [], "the wizard wrote a plan before the confirm tap"


@smoke("۹.۴")
async def test_the_confirm_page_previews_the_plan_and_says_it_is_not_active_yet(
    app: App,
) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await new_plan(admin, "۵۰ گیگ", "۳۰ روز", "نامحدود", 200_000)
    text = admin.last_text
    assert "هنوز برای مشتری فعال نیست" in text
    assert "حجم: 50 گیگ" in text and "مدت: 30 روز" in text
    assert "قیمت: 200,000 تومان" in text
    assert admin.buttons == ["✅ تأیید و فعال‌سازی", "✏️ تغییر عنوان", "❌ انصراف"]


@smoke("۹.۵")
async def test_changing_the_title_returns_to_the_confirm_page_with_the_new_title(
    app: App,
) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await new_plan(admin)
    await tap_starting(admin, "✏️ تغییر عنوان")
    await admin.send("پلن من")
    assert "پلن من" in admin.last_text and "هنوز برای مشتری فعال نیست" in admin.last_text
    assert "✅ تأیید و فعال‌سازی" in admin.buttons
    assert await all_plans(app) == []


@smoke("۹.۶")
async def test_cancel_returns_to_the_store_and_no_plan_exists(app: App) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await new_plan(admin)
    await tap_starting(admin, "❌ انصراف")
    assert "مدیریت فروشگاه" in admin.last_text
    assert await all_plans(app) == []
    assert await drafts(app) == 0
    # and the group does not list a plan either
    await tap_starting(admin, "پریمیوم")
    assert "پلن‌ها: 0" in admin.last_text


async def _live_plan_group(app: App):  # type: ignore[no-untyped-def]
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await set_panel_group(admin, "premium • #3")
    await new_plan(admin)
    return admin


@smoke("۹.۷")
async def test_activation_says_the_plan_is_live(app: App) -> None:
    admin = await _live_plan_group(app)
    await tap_starting(admin, "✅ تأیید و فعال")
    assert admin.popup == plain(texts.admin_store_plan_live)
    assert await live_plans(app, "پریمیوم") == ["۵۰ گیگ / ۳۰ روز"]
    assert await drafts(app) == 0


@smoke("۹.۸")
async def test_the_customer_sees_the_new_plan_at_once_without_any_publish_step(
    app: App,
) -> None:
    admin = await _live_plan_group(app)
    await tap_starting(admin, "✅ تأیید و فعال")
    ali2 = app.customer("sara")
    await ali2.start()
    labels = await shelf(ali2, "پریمیوم")
    assert any("۵۰ گیگ" in b for b in labels), labels


@smoke("۹.۸")
async def test_a_live_group_with_no_plan_does_not_crash_the_customer(app: App) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await set_panel_group(admin, "premium • #3")
    ali = app.customer("ali")
    await ali.start()
    labels = await shelf(ali)
    if "پریمیوم" in labels:
        await ali.tap("پریمیوم")
    assert app.unhandled_errors == [], app.unhandled_errors
    assert "پریمیوم" not in labels, "the dashboard promised the customer sees nothing"


@smoke("۹.۹")
async def test_a_price_change_reaches_the_customer_screen_immediately(app: App) -> None:
    admin = await _live_plan_group(app)
    await tap_starting(admin, "✅ تأیید و فعال")
    ali = app.customer("ali")
    await ali.start()
    labels = await shelf(ali, "پریمیوم")
    assert "200000" in digits(" ".join(labels)) or "۲۰۰" in " ".join(labels), labels
    await tap_starting(admin, "۵۰ گیگ")
    await tap_starting(admin, "💰 قیمت")
    await admin.send("260000")
    assert "260,000" in admin.last_text, admin.last_text
    bob = app.customer("bob")
    await bob.start()
    labels = await shelf(bob, "پریمیوم")
    joined = digits(" ".join(labels))
    assert "260000" in joined or "۲۶۰" in " ".join(labels), labels
    assert "200000" not in joined


@smoke("۹.۱۰")
async def test_a_second_edit_right_after_the_first_still_finds_its_group(app: App) -> None:
    admin = await _live_plan_group(app)
    await tap_starting(admin, "✅ تأیید و فعال")
    await tap_starting(admin, "✏️ گروه")
    await tap_starting(admin, "✏️ عنوان")
    await admin.send("پریمیوم نو")
    assert "یافت نشد" not in admin.last_text, admin.last_text
    assert "✅ ذخیره شد" in admin.last_text and "پریمیوم نو" in admin.last_text
    await tap_starting(admin, "✏️ عنوان")
    await admin.send("پریمیوم سه")  # and a third, on ids that are two versions old
    assert "یافت نشد" not in admin.last_text and "پریمیوم سه" in admin.last_text
    await tap_starting(admin, "⬅️ گروه")
    assert "پلن‌ها: 1" in admin.last_text, "the group lost its plan after an edit"
    assert await live_plans(app, "پریمیوم سه") == ["۵۰ گیگ / ۳۰ روز"]


@smoke("۹.۱۱")
async def test_a_group_takes_one_level_of_subgroups_and_refuses_a_second(app: App) -> None:
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    assert "➕ زیرگروه جدید" in admin.buttons
    await make_group(admin, "داخلی", kind="", parent="پریمیوم")
    assert "ساخته شد" in admin.last_text and "📂 داخلی" in admin.last_text
    assert "➕ زیرگروه جدید" not in admin.buttons, admin.buttons
    # A forged tap from a stale screen is refused too.
    view = await live_catalog(app)
    child = next(c for c in view.categories if c.title_fa == "داخلی")
    await admin.press(f"{StoreCB.CAT_SUB_ADD}{child.id}")
    await admin.send("نوهٔ نامجاز")
    view = await live_catalog(app)
    assert "نوهٔ نامجاز" not in [c.title_fa for c in view.categories], "a third level was made"
