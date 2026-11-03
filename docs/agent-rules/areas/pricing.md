---
paths:
  - "services/pricing.py"
  - "services/checkout.py"
  - "services/store_pricing.py"
  - "services/promo_rules.py"
  - "services/promo_redemptions.py"
  - "config/pricing*.json"
---

# Pricing

- **`services/pricing.py` imports no Telegram and no panel code.**
- **Money is integer toman** (`*_toman`). `Decimal` or ints, never `float`.
- **Fix the shared function, not each caller**: `should_lock_to_fixed_price`
  and `quote_checkout` are the one place a price is decided. The first was
  inlined twice once, and that is how every reseller paid retail (see
  `docs/agent-rules/areas/resellers.md`, which also holds the cross-over guard).

## Personal offers

- **Rule: the per-user extra percent is decided in ONE function,
  `services/checkout.py::personal_extra`, used by `resolve_price` and by the
  fixed-price lock in `quote_checkout`.** It returns `max(referral extra, the user's
  best live offer for this order)` and which offer supplied it. A reseller gets 0. On a
  tie the referral wins and the offer stays unused. Everything after it
  (`forbid_extra_discount`, `max_discount_percent`, `discount_combine`) is unchanged.
  **Why:** the offer is one more reason for the extra percent; a second copy of the
  decision is how the summary and the till would disagree.
  **Test:** `tests/unit/test_user_offers_checkout.py`, a mutation on the reseller
  exclusion.
- **Rule: a live offer is not voided, not expired, not held by a live order
  (`DEAD_ORDER_STATUSES` frees it, `PROVISION_FAILED` does not), and its
  `min_volume_gb` is strictly less than the order's volume** (unlimited never
  satisfies it). The queries live in `services/user_offers.py`.
  **Test:** `test_an_offer_on_a_dead_order_is_live_again_and_on_any_other_it_is_not`,
  `test_min_volume_is_refused_on_the_same_size_...`.
- **Rule: `price.meta["user_offer_id"]` is set only when the engine really applied the
  offer** (it supplied the extra, the plan takes extra discounts, the buyer is not on a
  reseller price, a promo code did not win the best-of). `create_pending_order`
  attaches it with one guarded UPDATE in the order's own savepoint; zero rows means
  `OfferNoLongerLive`, no order is written, and `confirm_order` answers
  `purchase_price_updated`.
  **Why:** two orders must never both spend one offer, and a customer must never be
  charged a price the offer no longer backs.
  **Test:** `test_create_pending_order_refuses_a_taken_offer_...`,
  `tests/integration/test_user_offers_race.py`, a mutation on the zero-row check.

## Promo codes

- **Checkout (`services/pricing.py::_apply_promo`) is the one place that
  refuses an inactive code.** The store admin's «غیرفعال» toggle writes only
  `active: false`. It once was not refused, and a switched-off code kept
  discounting.
- Seeds still store the bare `"10"` shape, so both shapes are read.
- **Rule: the usage rules of a code live in the pricing document entry
  (`first_purchase_only`, `per_user_limit`, `total_limit`, `expires_at`; all
  optional, the bare `"10"` and `{"percent","active"}` shapes mean none) and are
  decided in ONE function, `services/promo_rules.py::check_promo_eligibility`,
  called from `resolve_price` after the reseller drop and before pricing.** It
  raises one `PromoRuleError` subclass per rule, in the order expired, first
  purchase only, per-user limit, total limit, and says nothing about a code it
  does not find or finds inactive: `_apply_promo` stays the one place that
  refuses those.
  **Why:** the summary and the till both go through `quote_checkout`, so one call
  there makes them refuse alike; a second check in the handler would drift.
  **Test:** `tests/unit/test_promo_rules.py` (each rule, N-1/N/N+1, a mutation
  on each).
- **Rule: a use is a `promo_redemptions` row written by `create_pending_order`
  in the order's own savepoint, and the limits are counted again right after the
  insert.** The quote carries `meta["promo_redeem"]` only when the code was
  applied (`checkout._promo_was_applied`: under best-of the code counts when its
  percent is at least the extra percent, so exactly one of code and offer is
  used; under `sum` both). A redemption is live while its order is not in
  `DEAD_ORDER_STATUSES`; nothing rewrites it when an order dies. The key is the
  upper-case code string, never a catalog row id, so a publish keeps the counts; which rows
  count is decided by the next rule.
  **Why:** a read in the quote cannot see a second customer taking the last use;
  the recount inside the transaction can.
  **Test:** `tests/integration/test_promo_race.py`,
  `tests/unit/test_promo_rules.py::test_a_limit_filled_after_the_quote_but_before_the_write_rolls_the_order_back`.
- **Rule: a code counts its uses from the moment it was created.** The entry carries
  `counted_from` (aware ISO date-time), written by `upsert_draft_promo` when the code is not
  already in the draft and kept by every later save (percent, `PromoRules` replacement, the
  toggle); deleting a code and creating it again stamps a new one. Only redemptions with
  `created_at >= counted_from` count: `check_promo_eligibility` (quote), the recount in
  `record_redemption` (the quote's `promo_redeem` carries `counted_from` as an ISO string or
  None) and the admin list (`live_usage_by_code(since=...)`) all go through
  `count_live_redemptions`, the one definition of «live count». An entry with no readable
  `counted_from` (the bare `"10"`, `{"percent","active"}`, every code made before this) counts
  everything. It is a stamp, not a rule: the wizard never asks it and `_rule_keys` never writes it.
  **Why:** usage keyed on the code string alone made a re-created `WELCOME10` start from the
  deleted code's uses, and a customer who had used the old one was refused by the per-user limit.
  **Test:** `tests/unit/test_promo_counted_from.py`, `tests/unit/test_promo_counted_from_admin.py`,
  `tests/e2e/test_promo_recreated.py`.
- **Rule: `create_pending_order` reads `plan.id` once, before its lock-retry
  loop.** The retry rolls the session back, which expires `plan`; the second
  attempt then lazy-loaded it (`MissingGreenlet`), so two racing writers ended in a
  crash instead of the limit error.
  **Test:** `tests/integration/test_promo_race.py`.
- **Rule: every editor that rewrites a promo entry keeps the four rule keys (and `counted_from`), and a
  malformed rule is refused at the write** (`services/store_strategy.py::upsert_draft_promo`,
  `toggle_draft_promo_active`), so checkout never has to guess what a bad value meant.
  **Why:** the toggle once wrote only `active`; a rule that disappears when the code
  is switched off is a limit the owner thinks is on.
  **Test:** `tests/unit/test_promo_admin_rules.py`,
  `tests/e2e/test_promo_rules_admin.py`.
