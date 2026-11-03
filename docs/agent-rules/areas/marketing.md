---
paths:
  - "services/marketing.py"
  - "services/marketing_settings.py"
  - "services/marketing_run.py"
  - "services/volume_upgrade.py"
  - "scheduler/jobs/marketing.py"
  - "bot/handlers/marketing_admin.py"
  - "bot/keyboards/marketing.py"
  - "database/models/marketing.py"
  - "tests/integration/test_marketing_*.py"
---

# Marketing nudges

Four admin-switched messages to **retail** customers. Three of them may carry a
personal offer (see «Personal offers» below); the cart reminder never does. Every
campaign ships off. They are money-adjacent only in trust: a nudge to the wrong
person (a reseller, someone's walk-in customer) or one sent twice costs more
than it earns.

- **Rule: one function decides who may be nudged, `services/marketing.py::may_nudge`,
  and every campaign calls it.** Not a reseller, not banned, no order that
  references the service carries a `customer_label`, and for the trial message
  the order is a personal trial. The candidate queries do **not** filter these
  again: a second definition is how a former reseller's walk-in customer got a
  message.
  **Why:** `users.is_reseller` goes false when the flag is removed, but the label
  stays on every order that made the customer's service.
  **Test:** `tests/integration/test_marketing_eligibility.py`, a mutation on each
  condition.
- **Rule: a campaign acts only on events after `<name>_enabled_at`.** Switching on
  stores the moment, switching on while on keeps it, off then on starts a new one.
  Cart: `orders.created_at`; trial: the trial's `expires_at`; winback: the service's
  end; upgrade wording: the warning is sent after it.
  **Why:** switching a campaign on must not mail the whole backlog.
  **Test:** `tests/unit/test_marketing_settings.py`, `test_marketing_jobs.py`.
- **Rule: at most one capped nudge per user per 24 hours (`nudged_within_cap`), counted
  over delivered notices of `CAPPED_NOTICE_KINDS`; the cart reminder is exempt and
  uncounted; 30 sends per run.** Lifecycle notices and the volume warning (upgrade
  wording included) are not marketing and are not counted.
  **Test:** `test_marketing_eligibility.py`, `test_marketing_jobs.py`.
- **Rule: every nudge goes through `send_notice_once` with a stable `event_key`**
  (`mkt:cart:{order}`, `mkt:trial_convert:{user}`, `mkt:winback:{user}:{end date}`),
  claim, commit, send; the 90 day winback limit reads `sent_at`. A cart or
  trial-ended notice has no live service, so `service_notices.account_id` is nullable.
  **Test:** `test_marketing_jobs.py`, `tests/integration/test_marketing_migration.py`.
- **Rule: candidates for the cart and trial messages come from orders, never from live
  accounts, and neither calls the panel.** Lifecycle may have soft-deleted the
  trial's account by then. Winback is the one that reads the panel, once per
  candidate, and **never sends on a failed read** or on a status other than `expired`.
  **Test:** `test_a_trial_whose_service_lifecycle_already_deleted_is_still_nudged`,
  `test_the_panel_has_the_last_word_and_a_failed_read_never_sends`.
- **Rule: «another service still running» is read from the orders; a service with no
  delivered order is unknown and counts as running.** Never infer «ended».
- **Rule: the upgrade wording is a second wording of the volume warning, same event
  key, never a second message** (`services/volume_upgrade.py`, called from
  `volume_warnings._one` after the claim). It needs `may_nudge`, at least 40% of the
  period's time left (start = `activated_at` of the latest delivered non-trial order,
  end = the panel's expire already read; either missing is «not fast»), and a bigger
  enabled ready package in the service's own group, smallest first, found by slug;
  it shows no price. A failure there falls back to the standard wording.
  **Test:** `tests/e2e/test_marketing_upgrade.py`, a mutation on the fast-burn and
  the reseller conditions.
- **An idle run writes nothing** (`tests/integration/test_idle_jobs_read_only.py`).
  Reading the settings never creates the row.

## Personal offers

A discount for one user with no code, written with the nudge, taken by one order.

- **Rule: the offer row is written in the transaction that claims the notice, keyed by the
  notice's `event_key`** (`send_notice_once(offer=...)`, `claim_notice(offer=...)`). A
  retry after a released claim reuses the row and moves its `expires_at` to the new send
  plus the hours; a recipient who refuses the message for good has it voided
  (`voided_at`). Percent 0 writes no row and adds no line.
  **Why:** a row without a claim is a discount the customer never heard of; a claim
  without a row is a message that promises one.
  **Test:** `tests/integration/test_user_offers_nudges.py`, a mutation on the zero-percent
  and the void conditions.
- **Rule: the upgrade offer is written only for the upgrade wording**, decided before the
  claim in `volume_warnings._one`, with `min_volume_gb` = the service's current limit in
  whole GB. The standard warning never carries one.
  **Test:** `tests/e2e/test_user_offers.py` (rows ۱۷.۱۵).
- **Rule: an offer is never promised to a user who has an open order.** `may_nudge(...,
  require_no_open_order=True)` (`services/orders.py::user_has_open_order`, an id-only read of
  `OPEN_ORDER_STATUSES`) is passed by the trial-ended and winback campaigns, not by the cart
  reminder, which is about that order. The candidate is skipped for the run (no claim, no row,
  no budget used) and looked at again the next one. The upgrade wording of the volume warning
  still goes out, but with no offer row and no line (`volume_warnings._offer_may_be_promised`).
  **Why:** an open order is resumed at its old price, so the promised discount could not be used.
  **Test:** `tests/integration/test_user_offers_rules.py`, `tests/e2e/test_user_offers_rules.py`,
  a mutation on each of the three call sites and on the check itself.
- **Rule: at most one upgrade offer per service per period.** A period is `(data limit, expiry)`;
  every threshold of it shares `volume_warnings.period_key_pattern(account, period)`. A not-voided
  `upgrade` offer under another key (used or unused, expired or not) means the wording goes out
  with no row and no line (`user_offers.unvoided_offer_exists`); the key being claimed is
  excluded, so a retry of the same warning keeps its row and its line.
  **Why:** the key is per threshold, so two thresholds wrote two offers and the customer could
  spend both on two orders.
  **Test:** `tests/e2e/test_user_offers_rules.py`, a mutation on the condition and on the
  excluded key.
- **Rule: a spent offer is not promised again.** A volume warning comes back under the same
  key after a periodic reset (same limit, same expiry); when an order that is not dead holds the
  row of that key (`user_offers.offer_is_spent`), the wording goes out with no line and the row
  is left alone.
  **Why:** `write_offer` only moved the expiry of the row an order had taken, so each month's
  message promised a discount that was gone.
  **Test:** `tests/e2e/test_user_offers_rules.py::test_a_warning_sent_again_after_a_reset_does_not_promise_an_offer_an_order_took`,
  a mutation on the check.
- **Rule: an offer is live only while its message is out.** A transient send failure voids the
  offer of that `event_key` in the same transaction as `release_notice` (both branches:
  `send_notice_once`, `volume_warnings._one`); `write_offer` on the retry clears `voided_at` and
  moves `expires_at`, never `order_id`. A message refused for good is `undelivered`, is never
  claimed again, so its void is never cleared.
  **Why:** a customer who never saw the message must not hold the discount.
  **Test:** `tests/integration/test_user_offers_rules.py`, `tests/e2e/test_user_offers_rules.py`,
  a mutation on each `void_offer` call and on the `voided_at` clear.
- **Rule: the two controls per campaign (`bot/handlers/marketing_admin.py`) are percent
  0..50 and hours 1..168, typed in an FSM state and checked in
  `services/marketing_settings.py`; hours show only while the percent is above 0.**
  **Test:** `tests/e2e/test_user_offers.py` (rows ۱۷.۱۴).

