---
paths:
  - "bot/admin_home.py"
  - "services/admin_home.py"
  - "bot/handlers/admin.py"
  - "bot/keyboards/admin*.py"
  - "services/admin_orders.py"
  - "services/customer_lookup.py"
  - "services/service_ops.py"
  - "tests/unit/test_admin_home_layout.py"
---

# Admin home, customer card, customer desk

Same principles as the store, owner's 2026-09-30 decisions. Every admin
message carries a keyboard (`tests/unit/test_admin_navigation.py`).

## Home

- **Layout**: status block + «💳 پرداخت‌ها (n)» alone on top (PRIMARY only
  while something waits) + four sections (💰 فروش و سفارش, 👥 مشتری‌ها,
  🛍 فروشگاه, ⚙️ تنظیمات) + «📢 پیام همگانی».
- **Rendered only by `bot/admin_home.py::admin_home_screen`.** /admin, the
  reply button and «🏠» all go through it.
- **The status is DB-only** (`services/admin_home.py`): pending and stale,
  today's sales from the finance report's own window, ⚠️ lines only when
  wrong. **No live panel check on this screen.**
- `tests/unit/test_admin_home_layout.py` pins that every button the old home
  had is reachable from exactly one place.

## Customer card

- **Stays one screen.** Sections would cost a tap per action.
- Switches read state + action. Trial reset, reseller prices and zeroing show
  only when they would do something.
- **Services and orders are one button each** («📱 سرویس‌ها (n)»,
  «🛍 سفارش‌ها (n)») opening a list of buttons. An order button
  («#۱۲ • فعال • ۱۵۰٬۰۰۰ • ۱۴۰۵/۰۷/۰۸», short status words, ≤ 40 chars) opens
  the order, whose «فهرست» returns to that page (`SRC_CUSTOMER` in
  `order_cb`).

## Resellers list

«name • n فروش • تست x/y» for the current Jalali month
(`services/trial.py::reseller_month_sales`, same sale rule as the finance
windows).

## Customer desk

Owner's review of 2026-09-30, built in five packages (P1–P5).

- **Admin switches show a mark and the action, not a state sentence.** Ban:
  «🚫 مسدود کردن» / «✅ رفع مسدودی»; reseller: «🏪 نماینده کردن» / «🏪 حذف نمایندگی»;
  service: «⛔️ قطع سرویس» / «✅ فعال‌سازی سرویس». The label depends on the current state.
  Why: owner, 2026-10-03 — the marks are easier to tell apart than the words
  («مسدود: نه — مسدود کن» was tried and reverted). Tests tap the exact label, never
  a partial like «مسدود:». The card's ban switch is uncoloured; the red «🚫 مسدود کردن»
  is on «👥 مشتری‌ها». Test: `test_smoke_s11b_button_colours.py` (۱۱ب.۷).
- **Ban asks each time**: «فقط ربات» or «+ قطع سرویس‌ها»; unban mirrors it.
  The mode and direction ride in the callback, so a double tap cannot flip one
  into the other. The order screen keeps its old one-tap ban.
- **`extend_days` writes `orders.expires_at`** for the account's ACTIVE
  orders. Renewal reminders and the «در حال انقضا» filter read that column;
  before this, a manual extension left reminders firing on the old date.
- **«در حال انقضا» and the broadcast's `expiring_soon` share one subquery**
  (`services/broadcast.py::expiring_users_exists`). Change the window once.
- **«🎁 ساخت سرویس» is idempotent by `orders.admin_request_key`** (unique).
  The confirm button carries the key; a replay or a racing tap resumes the
  same order, never a second service or debit. It is the customer's own wallet
  path (debit → approve → provision → `settle_wallet_provision_failure`); a
  gift is price 0 and so not a sale. No wallet option for a reseller.
- **Service support tools read the panel best-effort** (devices, last online,
  last subscription fetch): a failed read drops its line, never the screen.
- Payment-card editing: see `docs/agent-rules/areas/payments.md`.

## Order card

- **Rule: an in-place refresh of the order card goes through
  `_edit_text_or_caption`**, never a bare `edit_text`.
  **Why:** a card with a receipt is a photo; Telegram refused the edit, the
  error was swallowed, and after a good retry the owner still saw «ناموفق».
  **Test:** `tests/e2e/test_smoke_s06_rest.py::test_after_a_retry_the_owners_order_card_shows_the_new_state`.

## Screen after a typed answer

- **Rule: «edit, else send» goes through `bot/handlers/common.py::edit_or_send`**,
  which sends directly when the message is the person's own text.
  **Why:** every typed admin answer first tried to edit the admin's message;
  Telegram refused each one and 26 e2e opt-outs hid it.
  **Test:** `tests/e2e/test_smoke_s07b_reseller_prices.py::test_typing_a_plan_price_causes_no_rejected_telegram_call`.

## Wallet ledger

- **Rule: a ledger reason is shown through `services/wallet.py::wallet_reason_label`**;
  a new reason code gets its entry in `WALLET_REASON_FA`, and an unknown one
  reads «سایر».
  **Why:** the card's own map missed `checkout_debit` and fell back to the
  raw key, so every wallet purchase read as English on the customer card.
  **Test:** `tests/unit/test_wallet_reason_labels.py`.

## Panel connection result

- **Rule: a failed connection test is worded by
  `services/panel_settings.py::panel_error_reason_fa`**, from the kind of
  failure; it returns no exception text and no class name.
  **Why:** a wrong API key read «اتصال برقرار نیست: PasarGuardAuthError».
  **Test:** `tests/unit/test_panel_settings.py`, e2e rows ۱.۱ and ۱۴.۲.

## Private chats only

- **The bot handles updates from private chats only**
  (`bot/middlewares/private_only.py`, an outer middleware on the update). In a
  group it only posts; a button it posts there must be a link into the private
  chat, never a callback, because a callback from a group is dropped.
  **Why:** live, the bot answered the report group's service messages (joined,
  became a forum, each of the 8 new topics) with «این پیام رو نفهمیدم», and
  `/start`, `/admin` and the menu labels opened their screens in the group.
  **Test:** `tests/e2e/test_live_group_silence.py`.

## The request's session

- **`UserContextMiddleware` commits its own read and `last_seen_at` touch
  before the handler runs** (`_end_own_transaction`). Do not move the touch
  back into the handler's transaction.
  **Why:** live, «database is locked» on `UPDATE users SET last_seen_at`: the
  read opened a WAL snapshot, a scheduler job committed while the handler
  talked to Telegram, and SQLite refuses at once to upgrade a stale snapshot
  to a write (`busy_timeout` does not apply).
  **Test:** `tests/integration/test_last_seen_snapshot.py` (`two_sessions`).

## Order screens

- **Rule: an admin order screen shows no raw enum and no plan row name.** The
  list buttons use `keyboards/admin.py::order_status_fa` (the list text's own
  mapping), the payment state `handlers/admin.py::payment_status_fa`, both
  without a raw fallback; the plan line is `order.product_label_fa`, set by
  the loaders in `services/admin.py` with the sale report's naming.
  **Why:** live, «#25 | ACTIVE», «پلن: volume-5GB-0d-p30000-h2-g3» and
  «پرداخت: verified».
  **Test:** `tests/e2e/test_live_admin_order_screens.py`.

## Existing-service linking

The customer card's «🔗 اتصال سرویس موجود» opens the dedicated account-link
router: exact name → panel/local ownership preview → explicit nonce-bound confirm.
No customer message or service provisioning is implied. Successful mapping is
visible through the normal service list; replay is a no-op/expired preview.
Another owner's mapping is refused even if it changes after preview. Every new
screen retains customer/home navigation. See the Mirza migration area rules.

## Mirza source history

Imported customer cards expose a paginated read-only source history. Sales opens
the global quarantine queue; every mapped record can return to its customer.
Amounts use the batch's persisted source unit and shared toman formatter. Unknown
units stay visibly unknown. These screens cannot credit, approve or settle old
payments; current wallet/order history is independent. Regressions:
`tests/e2e/test_mirza_history.py`, `tests/integration/test_mirza_history.py`.
