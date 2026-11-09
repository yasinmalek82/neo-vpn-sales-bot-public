---
paths:
  - "services/bank_invoices.py"
  - "services/bank_sms.py"
  - "database/models/bank_sms.py"
  - "web/bank_sms.py"
  - "tests/unit/test_bank_sms*.py"
  - "tests/integration/test_bank_sms.py"
  - "tests/integration/test_fast_bank_recovery.py"
  - "tests/e2e/test_bank_sms.py"
  - "tests/e2e/test_bank_sms_fast_delivery.py"
  - "services/payments.py"
  - "services/payment_settings.py"
  - "services/wallet.py"
  - "tests/integration/test_wallet_stale_balance.py"
  - "bot/handlers/payment.py"
  - "bot/keyboards/admin_payment.py"
---

# Payments

## Blu SMS confirmation

- Exact amounts are permanently reserved per account; never recycle a reservation
  or infer a match by suffix alone. The parser accepts only the reviewed Blu credit
  format, converts rial exactly, and never matches the balance.
- No bank reference/account is present in this SMS. Deduplicate conservatively on
  account + amount + bank minute. The receiver uses its own secret and exact sender
  filter; no query tokens or customer-submitted evidence can approve a payment.
- The suffix is separate from the sale price and credited to the wallet exactly
  once on both manual and automatic confirmation, inside the approval transaction.
  Expired/rejected/mismatched transfers queue a manual-review report without revival.
- A bank approval commits before provisioning; existing durable recovery and delivery
  workers consume it. Do not create an in-memory fire-and-forget provisioning task.
- A fresh `PAYMENT_APPROVED` order may skip the recovery age only when its verified
  card payment references an approved bank event for that same order. In-progress
  or other orders retain the age guard; ownership locks and retry backoff always apply.
- Delivery and recovery workers poll every 5 seconds, with one non-overlapping job
  each. This is a polling interval, not a promise of completion within 5 seconds.
- Only the two workers' routine scheduler start/success INFO lines are filtered
  to avoid flooding logs; their warnings, errors and other messages remain.
- Tests: `test_bank_sms.py` in unit/integration/e2e, `test_bank_sms_http.py`.
  Fast-path and two-session regressions: `test_bank_sms_fast_delivery.py`,
  `test_fast_bank_recovery.py` and `test_fast_delivery_schedule.py`.

Payment approval is idempotent. Two quick taps on one money confirm are
serialised by the Dispatcher's `SimpleEventIsolation` (root `AGENTS.md`); the
idempotency key does the rest.

## Wallet balance and cached users

- A row lock does not refresh an existing ORM object. The wallet reads its
  balance as a separate scalar from the locked database row before credit or
  debit, preserving unrelated pending changes and the caller's transaction.
- Why: using a previously cached balance could overwrite another committed
  credit or refuse a debit despite a newer credit.
- Test: `tests/integration/test_wallet_stale_balance.py` exercises independent
  sessions, replay, rollback and pending fields on a temporary SQLite database.
  Production PostgreSQL behavior requires separate evidence on a disposable DB.

## A receipt does not depend on FSM state

- **Rule: a photo from a customer with no FSM state is a receipt when they
  have exactly one open order in PENDING_PAYMENT with a card amount due**
  (`_OrderAwaitingReceipt`). The state is a convenience; the order is the record.
- **Why:** a bottom-keyboard tap (or a restart: the storage is in memory) clears
  the state while the order stays waiting, so the receipt got «نفهمیدم» and
  the admins never saw it.
- **Test:** `tests/e2e/test_smoke_audit_arch.py::test_receipt_after_tapping_support...`
  and `tests/unit/test_receipt_without_state.py`.

## Activation and delivery are separate durable states

- Activation enqueues one `service:{order_id}:customer` delivery intent in the
  same transaction. Automatic purchase/approval/retry/trial/gift handlers use
  that intent; scheduler resumes it independently. ACTIVE alone does not prove
  Telegram received the service card.
- Repeated approval/retry does not enqueue a second intent or repeat referral
  settlement. A pending delivery can still be recovered by the worker.
- Delivery claims are conditional and leased per item; no DB transaction spans
  Telegram sending. A revoked admin cannot receive a queued receipt. Telegram's
  acceptance-before-local-ack crash window may repeat one notification; it
  cannot repeat provisioning, wallet debit or credit.
- Tests: `tests/e2e/test_delivery_recovery.py`, `test_delivery_queue.py`, and
  existing repeated approval/retry guards in `test_smoke_s04_rest.py`.

## Late receipt

A card receipt for an **EXPIRED** order is real money with no owner, and the
order does not come back: expiry already refunded its wallet portion and the
state machine has no way out of EXPIRED.

- **`services/payments.py::record_late_receipt` queues one outbox report**
  (`late_receipt:{order_id}`, payments topic). Independently, the handler
  enqueues the original photo/document once per admin in `delivery_outbox`.
  Failed sends remain retryable even when the financial report already exists;
  successfully sent recipients are not resent on customer replay. The customer
  gets `notify_late_receipt` every time.
- **The handler does not clear the FSM state after the notice.** The same
  photo sent again then comes back through it, gets `notify_late_receipt`
  again without a second message to an already delivered recipient. Clearing it
  sent the repeat to «نفهمیدم».
- Test: `tests/e2e/test_smoke_s15_late_receipt.py::test_the_same_photo_again...`.

- **«🏦 شارژ کیف پول (X)» is the existing manual wallet confirm, pre-filled.**
  X is re-read from the order as `price - wallet_applied`, the stamp is
  `late_receipt:{order_id}`, so the credit is `adjust_wallet_manually` and
  nothing else.
- **`late_receipt_credited` matches on the shared stamp.** The ledger key
  carries the admin's id, so without that match a second admin could pay the
  same receipt.

## Payment card

- **DB value per field, else `.env`** (`services/payment_settings.py`).
- **Every customer read site goes through `get_payment_info`.** A test fails
  if one reads `settings.payment_*` directly.
- Editing is owner-only.

## Receipt media type

Persist `receipt_media_type` with the Telegram file ID and use matching
sendPhoto/sendDocument on initial delivery and administrator reopen. Historical
rows default to photo; a historic document type cannot be inferred from its ID.
Tests: `tests/e2e/test_receipt_media_type.py`.
