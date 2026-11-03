---
paths:
  - "services/reseller.py"
  - "services/reseller_admin.py"
  - "services/checkout.py"
  - "services/customer_label.py"
  - "services/customer_lookup.py"
  - "services/service_names.py"
  - "services/provisioning.py"
  - "services/trial.py"
  - "bot/handlers/purchase.py"
  - "bot/handlers/customer_label.py"
  - "bot/handlers/referral.py"
  - "bot/handlers/account.py"
  - "database/models/reseller.py"
  - "tests/unit/test_reseller*.py"
---

# Resellers

A reseller is a mobile shop that buys at an agreed price and hands the
subscription link to **their own** customer, who never opens this bot. The
role is granted by hand from the customer card; there is no request form.

## Role and price

- **`users.is_reseller` is deliberately separate from `is_admin`.** Admin-ness
  is re-synced from `ADMIN_TELEGRAM_IDS` on every request, so the env owns it;
  being a reseller is granted in Telegram and the table owns it. One column
  with two masters is the bug that would follow from merging them.
- **`reseller_plan_prices` keys on the package slug, never its row id.**
  Publishing a catalog edit mints new ids.
- **Two pricing shapes**: a fixed price per ready plan, and a raw rate
  (toman/GB + toman/day) for custom volume. The rate is **not** a percent off
  retail, so changing a retail price cannot move reseller margin.
- **Never remove the cross-over guard to «simplify» pricing.** That
  independence is why it exists: retail volume is `rate × GB` minus a duration
  discount with no per-day term; the reseller formula adds one, so the two
  curves cross on long services. `quote_*` charges the **lower** price and
  reports the crossing to the errors topic.
- **An agreed price replaces retail.** Referral and promo percents do not
  stack on top of it; the discount was taken when the price was agreed.
- **`_resolve_price` is not the last word. Do not inline
  `should_lock_to_fixed_price` again.** `_apply_reseller_price` hangs off
  `_resolve_price` in `bot/handlers/purchase.py`, but
  `should_lock_to_fixed_price` can still replace its answer with the plan's
  retail price. That rule lived inline, twice, and the order-creation copy had
  no reseller exception, so every reseller paid full retail on every ready
  plan while each screen before it showed the agreed price. It is now one
  named function used by both call sites.
- **A reseller browses at their own price**: `ready_plans_keyboard` takes
  `prices_by_slug`. A shelf that disagrees with the till reads as broken.

## The admin's price screens

- **The admin's reseller rules live in `services/reseller_admin.py`, not in the handler.** Typed numbers
  (`parse_reseller_input`), which plans a matrix screen holds (`screen_cells`), the preview, apply and
  undo of a percent cut (`preview_cut`, `apply_cuts`, `undo_screen_cut`) and the status block. The bot's
  handlers in `bot/handlers/admin.py` and the Mini App (`web-panel.md`, «Resellers») call the same
  functions. **Why:** a second copy is how the web would stop agreeing with the bot.
  **Test:** `tests/unit/test_web_panel_resellers.py::test_a_web_write_leaves_the_rows_the_bots_handler_leaves`.
- **A percent cut and its undo are scoped to the plans on the screen.** Every group has a tab at a common
  duration, so the tab alone reaches other groups' agreed prices and undo snapshots.
  **Test:** `tests/unit/test_reseller_matrix.py::test_percent_op_only_touches_visible_tab`,
  `tests/unit/test_web_panel_resellers.py::test_an_undo_takes_back_exactly_the_cut_on_its_own_screen`.
- **A flat list's cut is written under the economy tier (`apply_cuts`: `DEFAULT_TIER if flat else tier`),
  whatever tier the admin's screen carries.** The flat list has no tier tabs and its prices are read
  under economy, but `rs_sel_tier` survives a change of group (nothing resets it), so an admin who tapped
  a tiered group's premium tab and then a flat group cut under `premium`: a row the reseller is never
  charged by, and an undo that looks under economy could not find.
  **Test:** `tests/unit/test_reseller_admin.py::test_a_flat_cut_is_written_under_economy_when_the_screen_state_says_premium`
  (red on `main`), `test_the_premium_tier_of_another_group_survives_the_switch_to_a_flat_group`.
- **A typed number the database cannot hold, or a digit such as «²» (`isdigit()` true, `int()` raises),
  is the «فقط یک عدد بفرست» line; the trial quota is a 32-bit column.** The percent prompt reads with
  `isdecimal()` for the same reason.
  **Test:** `tests/unit/test_reseller_admin.py` (`test_a_plan_price_the_database_cannot_hold…`,
  `test_a_rate_the_database_cannot_hold…`, `test_a_trial_quota_past_the_32_bit_column…`,
  `test_a_percent_like_a_superscript_digit…`).

## Trials

- **A reseller trial is a different operation, not a variant.** A customer
  takes one per store group, for themselves; a reseller hands out many, free
  to a monthly quota then charged from their wallet. `issue_reseller_trial`
  never touches `trial_claimed_at`, and its report is keyed on the **order**.
  Keyed on the telegram id, the outbox would swallow every giveaway after the
  first.
- **The quota counts orders in the current Jalali month, not a counter
  column.** It cannot drift from what happened, and it resets in the calendar
  the owner actually uses.

## Their customers

- **«سرویس‌های من» forks on the role**: a customer gets the plain list, a
  reseller gets a paged, searchable one labelled by *their* customer's name.
  Paging is a cost control too: the list asks the panel for live status once
  per row.
- **The name and the phone are asked in two steps, and only the name reaches
  the panel.** `Order.service_name` holds the Latin customer name and seeds
  the panel username (`reza_a7f3`), exactly as a customer's own name does;
  `Order.customer_label` is the full note («reza | 09123456789»). Asked in one
  box, «reza 09123456789» sanitizes to `reza_09123456789` and every username
  the shop later searches for in PasarGuard carries eleven stray digits. The
  split is the fix, not a tidier prompt.
- **An order with no name at all (a customer trial) gets
  `build_anonymous_panel_username()`**, not the old `neo_<order id>`, which
  was guessable and published the bot's order count to anyone with panel
  access.
- **The phone is normalized to ASCII digits when it is written**, by
  `services/customer_label.py`. «چهار رقم آخر» is the search the owner asked
  to keep, and `LIKE` does not know ۶ and 6 are the same digit. Normalizing in
  the query instead would leave every row written before the fix unfindable.
  The query side still tries both forms, for names like «رضا ۲».
- **The two label steps live in `bot/handlers/customer_label.py`** and are
  shared by buying, trials and relabelling. `label_flow` in FSM data names the
  continuation; the continuations import lazily because all three callers
  would otherwise close an import cycle.

## Renewing

- **A reseller never gets the «تمدید یا جدید؟» question, and provisioning
  never guesses a renew for them.** `find_renewable_account` matches on plan
  shape, and a shop owns one service per customer, so «۵۰ گیگ / ۳۰ روز»
  matches last week's walk-in as readily as the person at the counter. A match
  there *extends that other customer's service*: the shop pays, their new
  customer gets nothing, and an unrelated customer quietly gains a month.
- **An explicit `renew_account_id` still renews**, because that is the shop
  naming a service in «مشتری‌های من» rather than the bot guessing.
- **That renew path skips the two label questions.** The service is already
  named; a retyped answer would overwrite `display_name` through
  `_finalize_active`, and a phone recalled from memory is how one customer
  becomes two rows.

## Panel username

**`_panel_username_for_order` reuses `order.panel_username` when it is set,
and `_provision_create` stamps it before calling the panel.** Both generators
end in a random suffix, so a `create_user` whose reply was lost used to be
retried under a *new* name and buy a second, orphaned panel account.
