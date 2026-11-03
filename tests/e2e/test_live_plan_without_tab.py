"""Live finding: a plan whose duration is not one of its group's tabs is invisible.

New group (shelf «تب مدت», only «۱ ماهه»), new plan «۵۰ گیگ بدون انقضا». The
dashboard said «🟢 همین را مشتری می‌بیند» and «پلن‌ها: ۱»; the customer saw «برای
این مدت پلنی نذاشتم». The tabs are the owner's setting, so the owner is told.
"""

from __future__ import annotations

from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s09_helpers import make_group, new_plan, open_store, set_panel_group, shelf
from tests.e2e.harness import App

WARNING = "مشتری نمی‌بیند"


async def _group(app: App):  # noqa: ANN202
    admin = app.admin()
    await open_store(admin)
    await make_group(admin, "پریمیوم")
    await set_panel_group(admin, "premium • #3")
    return admin


async def test_the_confirm_page_warns_that_the_duration_has_no_tab(app: App) -> None:
    admin = await _group(app)

    await new_plan(admin, "۵۰ گیگ", "♾ بدون انقضا", "نامحدود", 200_000)

    assert WARNING in admin.last_text, admin.last_text
    assert "بدون انقضا" in admin.last_text and "⏱ مدت‌ها" in admin.last_text
    assert "✅ تأیید و فعال‌سازی" in admin.buttons


async def test_the_dashboard_warns_and_the_customer_indeed_does_not_see_it(app: App) -> None:
    admin = await _group(app)
    await new_plan(admin, "۵۰ گیگ", "♾ بدون انقضا", "نامحدود", 200_000)
    await tap_starting(admin, "✅ تأیید و فعال")
    await open_store(admin)
    await tap_starting(admin, "پریمیوم")

    assert WARNING in admin.last_text, admin.last_text
    assert "⏱ مدت‌ها" in admin.last_text
    sara = app.customer("sara")
    labels = await shelf(sara, "پریمیوم")
    assert not any("۵۰ گیگ" in b for b in labels), labels


async def test_a_plan_on_an_existing_tab_gets_no_warning_and_is_seen(app: App) -> None:
    admin = await _group(app)
    await new_plan(admin, "۵۰ گیگ", "۳۰ روز", "نامحدود", 200_000)
    assert WARNING not in admin.last_text
    await tap_starting(admin, "✅ تأیید و فعال")
    await open_store(admin)
    await tap_starting(admin, "پریمیوم")

    assert WARNING not in admin.last_text, admin.last_text
    sara = app.customer("sara")
    labels = await shelf(sara, "پریمیوم")
    assert any("۵۰ گیگ" in b for b in labels), labels


async def test_the_flat_list_shows_every_duration_so_it_never_warns(app: App) -> None:
    admin = await _group(app)
    await new_plan(admin, "۵۰ گیگ", "♾ بدون انقضا", "نامحدود", 200_000)
    await tap_starting(admin, "✅ تأیید و فعال")
    await open_store(admin)
    await tap_starting(admin, "پریمیوم")
    await tap_starting(admin, "🛒 فروش")
    await tap_starting(admin, "فهرست ساده")
    await open_store(admin)
    await tap_starting(admin, "پریمیوم")

    assert WARNING not in admin.last_text, admin.last_text
    sara = app.customer("sara")
    labels = await shelf(sara, "پریمیوم")
    assert any("۵۰ گیگ" in b for b in labels), labels
