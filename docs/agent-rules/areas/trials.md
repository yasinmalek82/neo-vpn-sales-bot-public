---
paths:
  - "services/trial.py"
  - "bot/handlers/referral.py"
  - "database/models/order.py"
---

# Free trials

A trial belongs to a store group: `sales_flags.trial_gb` / `trial_days`, set
from the group dashboard's «🎟 تست» section. The owner sells direct unlimited
and tunnel volume, and a customer whose direct trial failed on their ISP must
still be able to test the tunnel before paying.

- **One per person per group**, enforced by
  `uq_orders_personal_trial_user_group` on `(user_id, trial_category_slug)`
  over live personal trials, not only in code.
- **The trial runs on the group's own panel groups**
  (`resolve_groups_for_plan_type` with the category id). A trial on any other
  group would not test the product.
- **A trial from before groups had trials is the direct one.** It counts
  against unlimited-kind groups only (`TrialOffer.counts_legacy`). So does
  `trial_claimed_at` with no trial order at all, which is what an import from
  the old bot leaves. `trial_claimed_at` is otherwise just «last trial taken»
  for the card.
- **Reset voids, it does not delete** (`orders.trial_voided_at`). Clearing
  `trial_claimed_at` alone once looked like a reset and did nothing: the old
  order still matched and the next tap resumed it.
- **Whole GB only.** Order and panel paths are GB-wide, and a fraction that
  rounds to 0 is read as *unlimited data*. Sub-GB trials need a change to how
  every account's limit reaches the panel; the owner chose 1 GB instead.
- **The device limit stays global** (`get_trial_hwid_limit`).
- **«تست نماینده» shows only while the group has a trial.** Without one it
  would be a switch nothing reads.
- Trial issuance is idempotent. A reseller's trial is a separate operation:
  see `docs/agent-rules/areas/resellers.md`.

- **Mirza's remaining counter <=0 imports as `users.legacy_trial_consumed`.**
  It blocks the same legacy direct entitlement without inventing a claim date;
  group/tunnel trials still use their shared rules. The existing admin reset
  clears the flag, and the customer card labels the historical date as unknown.
  Test: `test_old_trial_entitlement_is_preserved_and_admin_reset_can_release_it`.
