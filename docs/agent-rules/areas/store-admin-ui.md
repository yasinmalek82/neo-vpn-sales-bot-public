---
paths:
  - "bot/handlers/store_admin.py"
  - "bot/keyboards/store_admin.py"
  - "tests/unit/test_store_admin_layout.py"
---

# Store admin layout

Owner's 2026-09-30 decisions: simpler by arrangement, never by removing
options.

- **A group opens on a dashboard**: status block (sale, panel group, trial,
  shelf, plan count) with ⚠️ lines, its plans as buttons («۱۰۰ گیگ – ۳ ماهه |
  ۴۳۰ هزار», «⏸» when off sale), then four sections (🛒 فروش, 🎟 تست, 🖥 پنل,
  ✏️ گروه), each setting in exactly one of them.
- **Hide until relevant**: an option shows only when it would do something.
  Tiers and durations under the flat list, custom device limit without custom
  purchase, «تست نماینده» without a trial, «نوع» once a group has plans or
  children, premium panel group without tiers, «↕️ چیدمان پلن‌ها» under two
  plans, the seed button once the store has groups.
  `tests/unit/test_store_admin_layout.py` pins each rule.
- **A toggle is its mark and its name**: «✅ فروش جدید» on, «❌ فروش جدید» off,
  through `_toggle_label`. The owner took back «روشن — خاموش کن» on 2026-10-03:
  the marks are easier to tell apart at a glance.
- **The custom purchase setting is the one three-state toggle**: one button that
  cycles «❌ خرید سفارشی» → «🏪 خرید سفارشی: فقط نماینده‌ها» → «✅ خرید سفارشی:
  همه» (`_custom_mode_label`, beside `_toggle_label`, which stays two-state),
  and the dashboard says the state. Test: e2e row ۹.۲۲.
- **After a save, the same section re-renders with «✅ ذخیره شد».** ⬆️⬇️ live
  only on the plan-order screen.
- **Retired callbacks route, never spin.** Advanced, all plans, group list and
  old settings go to the hub or the group dashboard.
- **Rule: a typed plan dimension is checked at its question with
  `services/store_admin.py::check_dim`** (`DIM_LIMITS`, the same call the
  write makes), and the refusal says the range.
  **Why:** `150000` typed at the volume question went four steps on and was
  refused only at the final confirm.
  **Test:** `tests/e2e/test_smoke_s05_custom.py` (rows ۵.۴, ۵.۵).
- **Rule: duration tabs are the owner's setting, so a plan outside them is
  warned about, not given a tab**: the dashboard lists
  `services/store_catalog.py::plans_without_tab` and the wizard's confirm page
  checks `duration_has_tab`. Both say where to fix it («⏱ مدت‌ها» or
  «فهرست ساده»).
  **Why:** live, «۵ گیگ بدون انقضا» in a group whose only tab was «۱ ماهه»:
  the dashboard said «پلن‌ها: ۱» and the customer saw «برای این مدت پلنی
  نذاشتم».
  **Test:** `tests/e2e/test_live_plan_without_tab.py`.
- **Rule: the group dashboard's lines and ⚠️ warnings, the hub's status line, the
  marks in front of a group name and the plan's retail cell are composed in
  `services/store_dashboard.py`; the handler only formats them.** The Mini App
  shows the same screens through the same functions.
  **Why:** a second copy in a template is how two screens disagree about whether a
  group is for sale. **Test:** `tests/unit/test_store_admin_layout.py`,
  `tests/unit/test_web_panel_store.py::test_the_dashboard_is_the_bots_dashboard`.
- **Rule: which control a section shows is `services/store_group_settings.py::visible_controls`;
  the keyboards in `bot/keyboards/store_admin.py` ask it, they hold no copy of the
  condition.** The Mini App's group page asks the same function.
  **Why:** two copies of «hide until relevant» is how the bot and the page would
  disagree about what a group can set.
  **Test:** `tests/unit/test_store_admin_layout.py`,
  `tests/unit/test_web_panel_store_settings.py::test_a_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it`.

- **Rule: a price 10 times the plan's current price or a tenth of it is asked about once in the
  bot too** (`store_waiting_pkg_price_confirm`, the keyboard `store_price_jump_keyboard`), and
  saved on «✅ بله، همین قیمت». The rule is `services/store_edit.py::price_jump`; the bot's
  typed price, the Mini App's price cell and any later caller of `set_package_price` get it.
  **Why:** an extra zero in a price goes live at once. **Test:** `tests/unit/test_price_jump.py`.
- **Rule: a plan's dimensions, derived title, notes and the no-tab warning, and a group's
  creation, sub-group and deletion text, live in `services/store_plan_edit.py`; the wizard,
  the plan screen and the order screen call them.** The Mini App's editor calls the same.
  **Why:** two copies of a range or a note is how the bot and the page would differ.
  **Test:** `tests/unit/test_web_panel_store_plans.py::test_a_web_write_leaves_the_state_and_log_the_bots_handler_leaves`.
