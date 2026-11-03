---
paths:
  - "services/store_*.py"
  - "bot/handlers/store_admin.py"
  - "bot/keyboards/store_admin.py"
  - "bot/handlers/purchase.py"
  - "bot/keyboards/purchase.py"
  - "database/models/store.py"
  - "database/models/plan.py"
  - "tests/unit/test_store_admin_flow.py"
  - "tests/unit/test_flat_plan_ladder.py"
---

# Catalog

## Model

Two levels, adaptive: a group may hold **child groups or plans, never both**.

```
حجمی ─┬─ ویژه     ── ۵۰ گیگ / ۳۰ روز — ۱۵۰٬۰۰۰   (panel_group_ids = 11)
      └─ اقتصادی  ── ۵۰ گیگ / ۳۰ روز —  ۹۰٬۰۰۰   (panel_group_ids = 22)
نامحدود ───────────  ۱ کاربره / ۱ ماه — ۲۵۰٬۰۰۰   (no children → no extra tap)
```

- `StoreCategory.parent_id` nests one level (`MAX_CATEGORY_DEPTH = 2`).
- **A ready plan must carry dimensions in `options`** (`volume_gb` +
  `duration_days`, or `concurrent_users` + `duration_months`). Without them
  `choose_ready_plan` diverts the customer into the dynamic builder and the
  fixed price is discarded.
- **`sales_flags.has_time_limit` is derived from the duration.** Never set it
  directly.

## The store group is the only place a panel group is set

Its own `panel_group_ids`, else its parent's; premium tier on the same screen.

- Why: there was a product-type map and a default (DB and env
  `DEFAULT_PANEL_GROUP_IDS`) behind it. The owner set those, every order kept
  going to the store groups' seeded `[1, 2]`, and a volume trial landed on the
  unlimited group's servers. Both are gone from the screen and from
  resolution; the columns are unread.
- It is set from a **picker of the panel's own groups** (fetched once on open,
  several allowed, typed ids as the fallback when the panel is down). The old
  «one id, typed» prompt is gone (owner, 2026-09-30).
- **A group with no panel group is not for sale**: hidden from the menu and
  trials, refused at checkout, and `_provision_create` / `_provision_switch`
  raise `no_panel_group` before any panel call. Never a silent default again.

- **Rule: the گیگ/ماه builder counts as something to sell only when the
  pricing document carries its custom plan** (`custom_purchase_available`,
  the one test for the shelf, the «سفارشی» button and the builder itself).
  **Why:** the toggle defaults to on, so a new group with no plan sat on the
  customer's shelf and the tap raised `KeyError: 'volume_plan'`.
  **Test:** `tests/unit/test_empty_sale_category.py`.

- **Rule: who sees the custom builder is `custom_purchase_offered`, and nothing
  else asks.** The group's setting has three states (`sales_flags.
  custom_purchase_mode`: off, resellers, everyone; an old
  `allow_custom_purchase` bool reads as everyone / off, and the old key is
  written alongside as true only for everyone). The shelf, the «🧩 خرید سفارشی»
  button, the group list, a group with no ready plans and **every step of the
  builder** (volume tap, typed volume, days, users, months) ask it for THIS
  buyer, so a stale or forged callback cannot open the builder for someone who
  is not offered it. A reseller is offered it only with a per-GB rate
  (`price_per_gb_toman > 0`): without one `quote_custom_volume` would charge
  retail and a shop would pay it unannounced.
  **Why:** the owner wanted resellers to buy custom while retail customers do
  not, and a custom-only group would otherwise sit on a retail customer's menu
  as a dead end.
  **Test:** `tests/e2e/test_reseller_custom_purchase.py`,
  `tests/unit/test_custom_purchase_offer.py`.
- **Rule: a typed custom volume is a whole number from
  `CUSTOM_VOLUME_GB_MIN` to `CUSTOM_VOLUME_GB_MAX` (1 to 1000), checked by
  `parse_custom_volume_gb` at the volume question, which refuses with the range
  and keeps the buyer there.** It goes through the same price function and
  order fields as a quick button. It is the customer's limit, not
  `DIM_LIMITS["volume_gb"]` (10 000), which is the size of a plan the owner
  sells.
  **Why:** an unbounded typed volume would be priced and sent to the panel
  as typed.
  **Test:** `tests/e2e/test_reseller_custom_purchase.py` (refusals),
  `tests/unit/test_custom_purchase_offer.py`.

## Edits go live on confirm

There is no draft/publish cycle in the UI. Each confirmed edit calls
`commit_and_activate()`, which commits and publishes in one step. The owner's
verdict on the old two-trip flow: «این دردسر و اضافه‌کاری پیش‌نویس و انتشار
دیگه نباشه».

The versioned schema stays (it is the transaction boundary and the only
reason «بازگردانی از تاریخچه» works), but `StoreCB.PUBLISH` / `DRAFT` /
`DRAFT_DISCARD` and their handlers are gone. **Do not reintroduce a publish
button.**

- **A browse screen reads `get_catalog_view()` and nothing else.** It is the
  read-only path and never creates a draft. `get_or_create_draft()` and
  `get_draft_*` are for the write that follows a tap. Why: five browse screens
  called `get_draft_*` / `list_draft_*` / `preview_draft` and committed, so
  merely looking minted a draft and the preview told the owner his live edit
  was «هنوز منتشر نشده».
- **The new-plan wizard writes nothing until «✅ تأیید و فعال‌سازی».** A
  pending plan lives in FSM state, because a half-made plan sitting in the
  draft would ride along with the next unrelated edit and go live on its own.
- **Slug is the identity, not the row id.** Publishing clones the version and
  mints new row ids, so every button already on screen carries an id from the
  version before. `CatalogView.category()` / `package()` and
  `get_draft_category()` / `get_draft_package()` all fall back to the slug.
  This is not optional politeness: without it, «پلن‌های این گروه» renders
  empty after any edit, which is exactly how it shipped broken once.
- **Every `commit_and_activate` call passes a Persian `notes=`** describing
  the edit. The version chain **is** the change log («تغییرات فروشگاه» under
  پیشرفته) and that string is read straight back to the owner, so an internal
  English note is a leak.
- **Undo restores the predecessor of the tapped entry and is itself logged**,
  so it can be undone in turn.
- **Rule: the log text, the confirm sentence and the undo with its note are
  `services/store_changes.py`, called by the bot's handlers and the Mini App.**
  An entry is the version row (`ChangeEntry.version_id`): its id survives later
  publishes (a replaced version is archived in place), and a pruned entry is
  refused as stale, never read as another one.
  **Why:** the «N تغییر بعدی هم برمی‌گرده» sentence lived in the handler; a
  second screen would drop it. **Test:**
  `tests/unit/test_web_panel_store_changes.py`, `tests/unit/test_store_change_log.py`.
- **Undo reverts everything after it too**, because a version is a snapshot
  and not a diff. `ChangeEntry.undoes_also` is that count and the confirm
  screen must show it. Do not ship an undo button that hides this.
- **Pruning keeps entries for `STORE_CHANGE_RETENTION_DAYS` (7) or the newest
  `KEEP_ARCHIVED_STORE_VERSIONS` (5), whichever keeps more.** Age alone empties
  the log after a quiet fortnight and takes undo with it.
- **A group with children shows them as buttons.** The detail screen once
  counted «زیرگروه‌ها: ۲» with no way in, so «ویژه» was unreachable after the
  first visit. `tests/unit/test_store_admin_flow.py` pins it.

## Flat plan ladder («فهرست ساده»)

`sales_flags.flat_plan_list` drops the duration tabs and the tier row: every
ready plan of the group in one ascending list, **each at its own duration**
(the ladder is «۳۰ گیگ / ۱ ماه → ۵۰ گیگ / ۲ ماه → ۱۰۰ گیگ / ۳ ماه»). The owner
asked for it to cut the customer's decisions and make the bigger plan read as
the cheaper one per GB.

- **The price comes from the tapped plan's own duration cell**
  (`use_selected_duration=False`), never from the FSM tab. Charging the tab's
  duration would bill a 90-day plan at its 30-day cell.
- **The label is built from `options`, not `title_fa`**:
  `۱۰۰ گیگ – ۳ ماهه | ۴۳۰ هزار (گیگی ۴٬۳۰۰)`. En dash (a hyphen beside a
  number reads as minus), «٬» thousands separator, no «تومان» (the intro says
  it once). `tests/unit/test_flat_plan_ladder.py` caps it at 40 characters;
  the owner checked it uncut on a phone at that length.
- **Rule: the flat ladder's button text is `shelf_plan_label` (services/store_catalog.py),
  called by `ready_plans_keyboard` and by the Mini App's plan preview.**
  **Why:** the preview must show what the customer sees. **Test:**
  `tests/unit/test_web_panel_store_plans.py::test_the_preview_is_the_label_on_the_customers_real_keyboard`.
- The admin plan list warns when a bigger plan is not cheaper per GB. One step
  up in «گیگی» and the ladder stops selling the big plan.
