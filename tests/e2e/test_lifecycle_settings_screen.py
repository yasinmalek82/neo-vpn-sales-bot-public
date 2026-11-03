"""Section ۱۶, rows ۱۶.۱ to ۱۶.۵: the owner's screen for warnings and deleting ended services."""

from __future__ import annotations

from bot.keyboards.lifecycle import CODE_OF_FIELD, LifeCB
from bot.texts import texts
from tests.e2e._s16_helpers import lifecycle_row, open_lifecycle, set_number
from tests.e2e.harness import App, smoke
from tests.e2e.harness.expectations import report_plain


@smoke("۱۶.۱")
async def test_the_screen_shows_the_defaults_and_the_env_days_and_writes_nothing(
    app: App,
) -> None:
    admin = app.admin(0)
    await open_lifecycle(admin)
    text = admin.last_text
    assert "هشدار روز: 3 و 1 روز مانده" in text
    assert "هشدار حجم: 20٪ و 5٪ باقی‌مانده" in text
    assert "حذف خودکار: خاموش" in text
    assert "حذف بعد از انقضا: 10 روز" in text
    assert "حذف بعد از اتمام حجم: 10 روز" in text
    assert "حذف تست رایگان: 1 روز بعد از پایانش" in text
    assert "هشدار پیش از حذف: 3 روز قبل از حذف" in text
    assert "آخرین تمرین: هنوز اجرا نشده" in text
    # State first: ✅ for what is on, the value in the label for a number.
    assert "✅ هشدار روز: ۳ ۱" in admin.buttons
    assert "✅ هشدار حجم: ۲۰٪ ۵٪" in admin.buttons
    assert "✅ حذف پس از انقضا: ۱۰ روز" in admin.buttons
    assert "✅ خاموش" in admin.buttons
    assert await lifecycle_row(app) is None, "looking at the screen writes nothing"


@smoke("۱۶.۲")
async def test_a_typed_number_is_checked_where_it_is_typed_and_saved_when_right(
    app: App,
) -> None:
    admin = app.admin(0)
    await set_number(admin, "delete_after_expiry_days", "366")
    assert "365" in admin.last_text and admin.last_text.startswith("❌")
    assert (await lifecycle_row(app)) is None, "a refused number stores nothing"
    # Still waiting: the next message is another try.
    await admin.send("۲۰")
    assert "ذخیره شد" in admin.last_text
    assert "حذف بعد از انقضا: 20 روز" in admin.last_text
    row = await lifecycle_row(app)
    assert row is not None and row.delete_after_expiry_days == 20


@smoke("۱۶.۲")
async def test_zero_turns_a_number_off_and_a_list_is_read_in_either_digits(app: App) -> None:
    admin = app.admin(0)
    await set_number(admin, "delete_after_limited_days", "0")
    assert "❌ حذف پس از اتمام حجم" in admin.buttons
    await set_number(admin, "warn_days", "۷ ۲")
    assert "✅ هشدار روز: ۷ ۲" in admin.buttons
    await set_number(admin, "warn_volume_percents", "0")
    assert "❌ هشدار حجم" in admin.buttons
    row = await lifecycle_row(app)
    assert row is not None
    assert (row.delete_after_limited_days, row.warn_days, row.warn_volume_percents) == (
        0,
        [7, 2],
        [],
    )


@smoke("۱۶.۲")
async def test_the_prompt_states_the_range_and_can_be_left(app: App) -> None:
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.press(f"{LifeCB.EDIT}{CODE_OF_FIELD['warn_volume_percents']}")
    assert "1 تا 99" in admin.last_text and "0 یعنی خاموش" in admin.last_text
    assert "❌ لغو" in admin.buttons and "🏠 خانه ادمین" in admin.buttons
    await admin.tap("❌ لغو")
    # Out of the state: a typed word is not read as a number any more.
    await admin.send("۵")
    assert (await lifecycle_row(app)) is None


@smoke("۱۶.۳")
async def test_on_is_refused_without_a_rehearsal_and_the_message_says_why(app: App) -> None:
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("روشن")
    assert (
        report_plain("تمرین") in admin.last_text and "روشن کردن حذف خودکار رد شد" in admin.last_text
    )
    row = await lifecycle_row(app)
    assert row is None or row.delete_mode == "off"
    assert admin.buttons, "the refusal carries a keyboard"


@smoke("۱۶.۳")
async def test_rehearsal_can_be_switched_on_and_off_freely(app: App) -> None:
    admin = app.admin(0)
    await open_lifecycle(admin)
    await admin.tap("👁 تمرین")
    assert "✅ 👁 تمرین" in admin.buttons
    assert "تمرین هر روز اجرا می‌شه" in admin.last_text
    await admin.tap("خاموش")
    assert "✅ خاموش" in admin.buttons
    assert texts.admin_life_note_off.strip() in admin.last_text


@smoke("۱۶.۴")
async def test_a_helper_admin_sees_the_screen_but_cannot_change_a_number(app: App) -> None:
    helper = app.admin(1)
    await open_lifecycle(helper)
    assert "هشدار و حذف سرویس" in helper.last_text
    assert not any(b.startswith(("✅", "❌")) for b in helper.buttons if "هشدار" in b)
    # A forged tap is refused as well.
    await helper.press(f"{LifeCB.EDIT}{CODE_OF_FIELD['delete_after_expiry_days']}")
    assert helper.popup == texts.admin_life_not_owner
    await helper.press(f"{LifeCB.MODE}rehearsal")
    assert helper.popup == texts.admin_life_not_owner
    assert (await lifecycle_row(app)) is None
