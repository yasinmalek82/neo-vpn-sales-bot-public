---
paths:
  - "services/provisioning.py"
  - "services/provision_recovery.py"
  - "database/provision_lock.py"
  - "database/models/provision_recovery.py"
  - "scheduler/jobs/provision_recovery.py"
  - "services/renewal.py"
  - "services/group_sync.py"
  - "services/service_ops.py"
  - "services/orders.py"
  - "services/compensation.py"
  - "bot/keyboards/purchase.py"
  - "tests/unit/test_plan_user_limit.py"
  - "tests/integration/test_sh_durability.py"
---

# Provisioning, renewal and panel writes

Panel writes read first and write an **absolute** value, so a retry after a
timeout cannot double-apply. Provisioning and renewal are idempotent. A test
named `test_sh*` or living in `tests/integration/test_sh_durability.py` marks
a guard that closed a money bug.

## Panel calls and the SQLite snapshot

- **Rule: no read snapshot is held across a panel call**
  (`_release_before_panel_call`), and the order is read again before it is
  written (`_reread_after_panel_call`): one that is no longer PROVISIONING
  raises `order_changed` and is left alone. The same shape guards
  `service_ops.extend_days` / `add_data` (the pending row is looked up again
  after the live read) and `revoke_link`.
  **Why:** the re-read after the intent commit pinned a WAL snapshot; a
  scheduler commit during the panel call made the next write fail at once
  («database is locked»). The panel had already created the service, and the
  customer who paid got «یه مشکلی پیش اومد» and no card until a retry.
- **The release never commits a transaction that has written.** A resumed
  order's retry debit is flushed by the caller and must roll back with a
  failed retry (`test_a_failed_admin_retry_does_not_keep_the_retry_debit`).
  **Test:** `tests/integration/test_provision_snapshot.py`,
  `tests/integration/test_service_ops_snapshot.py`.

## User (device) limit

A volume plan's limit is `options["hwid_limit"]`; custom volume uses the
group's `sales_flags["volume_hwid_limit"]`; both default to **0**. It was a
hard-coded `or 1` (one device) that the owner could neither see nor change.

- **«نامحدود» is sent as `0`, never `None`.** PasarGuard
  (`app/operation/subscription.py::is_hwid_enabled`): `0` turns the device
  check off even under a forced policy; `None` falls back to the panel's
  global setting.
- **Never write a fallback as `int(x or 1)`.** `0 or 1 == 1`, so it silently
  turns «نامحدود» into one device. `tests/unit/test_plan_user_limit.py` goes
  red on it.
- **The shelf says «محدودیت تعداد کاربر ندارن» only when every plan shown has
  limit 0.** A rule the bot states, it must keep.
- **A renewal's `UserModify` sends the `hwid_limit` the account was created
  with** (`_hwid_limit_service_was_created_with`), never today's plan value.
  Leaving it out would hand the account to the panel's global policy, the
  `None` case above. `test_early_renew_extends_from_current_expire` goes red
  without it.

## Plan switch on the same account

A service whose plan is gone (every account imported from the old bot) or a
customer moving products (direct unlimited → tunnel volume) picks any plan
from the store, applied to **that** account: same username and link. The
renew summary also offers «🔁 پلن دیگه روی همین سرویس».

- **Never infer a switch from a slug mismatch.** `orders.renew_resets` marks
  it, set only when the customer chose it on a summary that says the leftover
  is dropped. A re-created plan would otherwise silently wipe someone's
  leftover.
- **`_provision_switch` resets, it does not extend**: expiry = today + days,
  usage reset, the new plan's volume, panel groups and device limit. Targets
  are committed first, then `reset_user_data_usage`, then `modify_user`.
- **Its retry test is equality, not `expire >= target`.** A switch to a
  shorter plan lowers the expiry, and the extension test would call it
  applied.
- **Rule: that equality covers every value the switch writes**: expiry,
  volume, panel groups and device limit (`_switch_applied`).
  **Why:** with expiry and volume only, a same-size plan of another panel
  group on a service bought minutes ago read as applied; no `modify_user`.
  **Test:** `tests/unit/test_plan_switch.py::test_a_same_size_switch_to_another_panel_group_is_written`.
- **A switch never guesses an account by plan shape.** If the named account is
  not theirs or is gone, a new service is created.
- **After a switch, a same-plan renew sends the switch's device limit**
  (`_hwid_limit_service_was_created_with` prefers the latest applied switch).
- The name questions are skipped: the service keeps its name.

## The three operations on an existing account

| Operation | Marker | Expiry | Volume | Usage | Panel groups | Device limit |
| --- | --- | --- | --- | --- | --- | --- |
| extend (`_provision_renew`) | none (`renew_account_id`) | later of now / current + days | **added** to what is left | kept | never | the one it was made with |
| switch (`_provision_switch`) | `renew_resets` | now + days | the new plan's | reset | the new plan's | the new plan's |
| from scratch (`_provision_from_scratch`) | `renew_mode = 'reset'` | now + days | the bought volume | reset | **never written** | the service's own |

«📅 تمدید این سرویس» is decided by one function, `services/renewal.py::resolve_renew`:
a ready plan that matches extends; a **volume service no ready plan matches**
(and not a trial) is renewed from scratch when the buyer can be priced (a
reseller with a per-GB rate, `has_per_gb_rate`; anyone else with the pricing
document's custom volume plan); anything else is a switch. A group with
«✅ تمدید» off refuses the first two (`category_renew_off`, the group and the
groups above it).

- **Rule: a renew from scratch is dispatched on `orders.renew_mode` alone.**
  Never inferred from a missing plan slug, a plan shape or a custom order: a
  slug-less custom order sent to «تمدید همان سرویس» from the buy menu still
  extends. **Why:** inferring it would wipe the leftover of a customer who only
  asked for more. **Test:** `tests/e2e/test_renew_from_scratch.py::
  test_buying_the_same_custom_shape_again_and_choosing_renew_still_adds`.
- **Rule: the from-scratch write never carries `group_ids`, and a switch on an
  account no order of ours made (imported from the old bot) does not either**
  (`account_orders.account_was_created_by_an_order`). **Why:** the owner gives
  an imported service its panel groups (`vip1`, `vip 2`, ...); a plan's
  group is not that, and writing it strips inbounds the customer paid for.
  **Test:** `test_the_write_never_carries_group_ids`, `test_an_imported_service_takes_
  its_defaults...`, `test_without_any_custom_plan_in_the_document...`.
- **Rule: the retry test is equality over every value written** (expiry within
  the tolerance, volume, device limit), **usage excluded** (`_scratch_applied`).
  Targets are committed first; the usage reset comes before the write. **Why:**
  once the first application is confirmed the customer is using the service
  again, and reading «used > 0» as «not applied» wipes it a second time. A reset
  that happened without the write converges by resetting once more. Known
  limit: an identical renew within ~2 minutes of the same shape reads as applied
  and leaves usage as it is (the switch has the same window).
  **Test:** `test_a_lost_reply_after_the_panel_applied_it_is_not_applied_twice`,
  `test_a_timeout_after_the_usage_reset_and_before_the_write_converges`.
- **Rule: the service keeps its device limit**, the same source an extension
  uses (latest applied switch, else the first order that built it, this order
  excluded); an account with no order keeps what the panel holds, and the order
  stores it so the next renew reads the real number.
- **Rule: a custom order is never matched to a ready plan by shape**
  (`orders.custom_purchase`), or a 50 GB custom service would renew as the 50 GB
  plan and add. A legacy order without the mark keeps the old shape match.
- **Rule: the renew of a service the buyer already has does not ask the group's
  «who sees custom» setting.** That setting decides who may start a new custom
  purchase; a retail customer of the old bot must be able to renew where custom
  is off for new sales.
- **Rule: a trial has no renew** (`account_orders.is_trial_account`: at least
  one delivered order references the account and every one is a trial). It gets
  «🛒 خرید اشتراک» instead, on every card.

## Changing a store group's panel group

A PasarGuard user gets its groups once, at create time; renew and revoke never
send them. So after the owner points a store group at a new panel group, the
bot asks whether to move the services already in it
(`services/group_sync.py`).

- **Only a user the panel shows on the old group moves.** The old groups ride
  in the confirm button, because after the save nothing else remembers them. A
  premium-tier user or one already switched elsewhere is on a third group and
  is counted as «دست نخوردن», never touched.
- **An account belongs by its latest ACTIVE order, not by any.** After a plan
  switch the old order stays ACTIVE, and counting it would pull the customer
  back onto the product they paid to leave.
- **Read first, absolute `group_ids`, one failure never stops the rest, and an
  empty target is refused**: it would strip every inbound.

## A wallet purchase the panel did not answer

- **Rule: a 5xx or timeout on a wallet checkout keeps the order PROVISIONING,
  tells the customer their money was taken, and queues
  `order:{id}:provision_unconfirmed` for the owner; the customer's next
  confirm continues that order** (`find_unfinished_wallet_order`), never a
  second one.
- **Why:** the customer was told «ثبت سفارش نشد», tried again as told, and was
  debited twice with two PROVISIONING orders. The one-open-order index only
  covers the two payment states.
- **Test:** `tests/e2e/test_smoke_audit_arch.py::test_panel_500_on_a_wallet_purchase...`
  and `test_a_second_confirm_after_a_panel_500_resumes_the_paid_order`.

## Autonomous provisioning recovery

- `provision_order` owns an order lock through the caller's final transaction,
  including its internal target commits. PostgreSQL uses a session advisory
  lock on a separate connection with no open transaction during panel IO;
  SQLite uses a local OS lock. Overlap raises `provision_busy` without panel IO.
  The owner must commit/rollback/close its session to release a completed attempt.
- Scheduler recovery reads only committed PAYMENT_APPROVED/PROVISIONING orders
  idle for at least one minute. It never calls the administrator retry-debit path
  on PROVISION_FAILED and never initiates a debit or refund. A failure rolls back
  uncommitted failed markers while retaining the durable paid intent.
- Retry metadata is committed before IO; each batch has at most five orders and
  each attempt has a 120-second budget. Backoff grows from one to thirty minutes.
  Fifteen-minute unresolved paid intents queue one actionable errors report,
  linked to the customer desk. A report is an intent until its outbox is delivered.
- Local SQLite proof is not PostgreSQL proof. Verify the advisory lock, release
  and transaction behavior against the explicitly authorized disposable server.
- Regressions: `tests/integration/test_provision_overlap.py` and
  `tests/e2e/test_provision_recovery.py` cover concurrent/caller-commit ownership,
  rollback release, lost create replies, retry backoff, aged alerts, nonpending
  exclusions and no automatic financial adjustment.
