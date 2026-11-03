"""Section 3: the free trial, one per person per store group.

MANUAL: ۳.۳ (open the link in a real client and load a site) needs a real VPN
client and a real panel node; what the bot controls, the panel group, is asserted
in ۳.۲ and ۳.۵.
STALE: ۳.۰ names «⚙️ تنظیمات بیشتر»; the group screen now has «🎟 تست» directly.
۳.۶ names «⚙️ تنظیمات پنل»; the button is «🔌 اتصال پنل» under «⚙️ تنظیمات».
"""

from __future__ import annotations

from sqlalchemy import select

from bot.texts import texts
from database.models import Order
from tests.e2e._s01_helpers import open_group_trial, set_group_trial
from tests.e2e._s07b_helpers import open_card, tap_starting
from tests.e2e.harness import GB, App, plain, publish_store, smoke
from tests.e2e.harness.expectations import report_plain

TRIAL = "🎟 تست رایگان"


@smoke("۳.۰")
async def test_the_group_trial_switch_takes_gb_and_days_and_reveals_the_reseller_switch(
    app: App,
) -> None:
    await publish_store(app, trial_gb=0)
    admin = app.admin()
    await open_group_trial(admin, "حجمی", "ویژه")
    assert admin.buttons == ["🎟 تست رایگان: خاموش — تغییر", "⬅️ گروه"], admin.buttons
    assert not any("تست نماینده" in b for b in admin.buttons), "no trial, no reseller trial"
    await tap_starting(admin, "🎟 تست رایگان: خاموش")
    await admin.send("1 1")
    assert "🎟 تست رایگان: ۱ گیگ • ۱ روز — تغییر" in admin.buttons, admin.buttons
    assert any(b.endswith("تست نماینده") for b in admin.buttons), admin.buttons
    # A customer sees the new trial at once.
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    assert app.panel.only_user().data_limit == 1 * GB  # the only trial group builds at once
    # And switching it off with 0 takes the reseller switch away again.
    await admin.send("/admin")
    await open_group_trial(admin, "حجمی", "ویژه")
    await tap_starting(admin, "🎟 تست رایگان: ۱ گیگ")
    await admin.send("0")
    assert admin.buttons == ["🎟 تست رایگان: خاموش — تغییر", "⬅️ گروه"], admin.buttons


@smoke("۳.۱")
async def test_with_two_trial_groups_the_customer_is_asked_which_and_sees_gb_and_days(
    app: App,
) -> None:
    await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    assert ali.last_text == plain(texts.trial_pick_group)
    assert ali.buttons[:2] == ["نامحدود | ۱ گیگ • ۱ روز", "ویژه | ۱ گیگ • ۱ روز"], ali.buttons
    assert app.panel.users == {}, "asking must not build anything"


@smoke("۳.۱")
async def test_with_one_trial_group_the_trial_is_built_without_a_question(app: App) -> None:
    store = await publish_store(app, trial_gb=0)
    await set_group_trial(app, store, "special", 1, 1)
    ali = app.customer("ali")
    await ali.start()
    mark = ali.mark()
    await ali.tap_reply(TRIAL)
    assert plain(texts.trial_pick_group) not in ali.texts_since(mark)
    assert len(app.panel.users) == 1
    assert ali.texts_since(mark)[0].startswith(
        report_plain(texts.trial_ready_intro.format(days="1", gb="1", group="ویژه"))
    )


@smoke("۳.۲")
async def test_the_trial_account_has_the_groups_size_days_panel_group_and_device_limit(
    app: App,
) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "ویژه")
    account = app.panel.only_user()
    assert account.data_limit == 1 * GB
    assert 0.9 < (account.days_left() or 0) <= 1
    assert account.group_ids == store.groups["special"].panel_groups  # tunnel, not direct
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "⚙️")
    await tap_starting(admin, "🔌")
    assert "دستگاه اکانت تست: 1" in admin.last_text
    assert account.hwid_limit == 1
    assert account.username in ali.last_text  # the card names the account


@smoke("۳.۴")
async def test_the_same_group_again_sends_the_customer_to_my_services(app: App) -> None:
    await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "ویژه")
    await ali.tap_reply(TRIAL)
    mark = ali.mark()
    await tap_starting(ali, "ویژه")
    assert ali.texts_since(mark) == [plain(texts.trial_see_my_services)]
    assert "📡 سرویس‌های من" in ali.buttons
    assert len(app.panel.users) == 1, "no second account"
    trials = await app.all(select(Order).where(Order.trial_kind.is_not(None)))
    assert len(trials) == 1


@smoke("۳.۵")
async def test_the_other_group_gives_a_second_trial_on_its_own_panel_group(app: App) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "ویژه")
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "نامحدود")
    assert len(app.panel.users) == 2
    assert sorted(tuple(u.group_ids) for u in app.panel.users.values()) == sorted(
        [tuple(store.groups["special"].panel_groups), tuple(store.groups["unlimited"].panel_groups)]
    )
    # A third tap on either one builds nothing more.
    for group in ("ویژه", "نامحدود"):
        await ali.tap_reply(TRIAL)
        await tap_starting(ali, group)
    assert len(app.panel.users) == 2
    assert len(app.panel.requests_to("POST /api/user")) == 2


@smoke("۳.۶")
async def test_panel_settings_keep_only_the_trial_device_limit(app: App) -> None:
    await publish_store(app)
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "⚙️")
    await tap_starting(admin, "🔌")
    screen = admin.last_text
    assert "دستگاه اکانت تست" in screen
    mentions = [line for line in screen.splitlines() if "تست" in line]
    assert len(mentions) == 1 and mentions[0].startswith("دستگاه اکانت تست"), mentions
    trial_buttons = [b for b in admin.buttons if "تست" in b and "اتصال" not in b]
    assert trial_buttons == ["🧪 دستگاه تست"], trial_buttons


@smoke("۳.۷")
async def test_allow_trial_again_makes_a_fresh_trial_not_the_old_one(app: App) -> None:
    await publish_store(app)
    ali = app.customer("ali")
    await ali.start()
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "ویژه")
    first_name = app.panel.only_user().username
    admin = app.admin()
    await open_card(admin, "ali")
    assert any("ویژه ✓" in line for line in admin.last_text.splitlines() if "تست:" in line)
    await tap_starting(admin, "🧪 اجازهٔ تست دوباره")
    assert any("ویژه —" in line for line in admin.last_text.splitlines() if "تست:" in line)
    assert "🧪 اجازهٔ تست دوباره" not in admin.buttons  # nothing left to reset
    await ali.tap_reply(TRIAL)
    mark = ali.mark()
    await tap_starting(ali, "ویژه")
    assert plain(texts.trial_see_my_services) not in ali.texts_since(mark)
    assert len(app.panel.users) == 2
    assert {u.username for u in app.panel.users.values()} > {first_name}
    assert len(app.panel.requests_to("POST /api/user")) == 2
    voided = await app.all(select(Order).where(Order.trial_voided_at.is_not(None)))
    assert len(voided) == 1, "the old trial is voided, not deleted"


@smoke("۳.۸")
async def test_every_trial_posts_one_message_to_the_customers_topic(app: App) -> None:
    await publish_store(app)
    group = await app.connect_report_group()
    ali = app.customer("ali", username="ali_u")
    await ali.start()
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "ویژه")
    await ali.tap_reply(TRIAL)
    await tap_starting(ali, "نامحدود")
    await app.run_job("report_flush")
    real = [t for t in await group.texts("customers") if "تست صادر شد" in t]
    assert len(real) == 2, real
    assert all("ali_u" in t for t in real)
