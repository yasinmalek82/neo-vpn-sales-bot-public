---
paths:
  - "services/service_cleanup.py"
  - "services/lifecycle_*.py"
  - "services/service_notices.py"
  - "services/volume_warnings.py"
  - "services/account_orders.py"
  - "scheduler/jobs/service_lifecycle.py"
  - "scheduler/jobs/volume_warnings.py"
  - "bot/handlers/lifecycle_admin.py"
  - "tests/unit/test_service_cleanup_decide.py"
  - "tests/unit/test_soft_deleted_accounts.py"
---

# Warnings, and deleting ended services

The owner: «be very careful, a mistake here is expensive for me». A missed
deletion is harmless; a wrong one destroys a paying customer's service and
cannot be undone. **Every choice falls on the side of not deleting.** The
feature ships `off`; `rehearsal` runs the job and deletes nothing; `on` is
refused unless a rehearsal reported in the last 7 days and changing a deletion
number voids that rehearsal.

## The decision (`services/service_cleanup.py::decide`)

- **Rule: DELETE only when ALL hold**: mode is `rehearsal`/`on`; the panel user
  was read in THIS run; not exempted (`delete_exempt`); panel status `expired`
  (expiry in the past) or `limited`; no order of that user is pending, in
  review, approved or provisioning (a `PROVISION_FAILED` one keeps only its own
  account); not created or changed in the last day; **more than N whole days**
  since it ended (10 days exactly, or 10d 23h, is KEEP; 11 is DELETE); N = 0
  turns the rule off.
- `active`, `on_hold`, `disabled`, unknown status, an `active` user with a past
  expiry, no expiry, a banned owner: always KEEP.
- **Warn first.** A paid service is DELETE only if its deletion warning was SENT
  at least `delete_warn_days_before` days ago for the current period (limit +
  expiry, so a renewal re-arms it); a service past its deadline that was never
  warned is WARN, not DELETE. A trial gets no warning. 0 turns the requirement off.
- **Limited clock**: `limited_since` is set the first time a run sees the user
  `limited`, cleared the first time it sees anything else, and a service is
  never deleted on volume in the run that first sees it limited.
- A trial is a service **only trial orders reference** (`services/account_orders.py`).
  Trials get no volume warning and no day reminder; a personal trial ending within
  a day gets one message of its own («تست رایگانت…»), a reseller's trial none.
- **Why:** each line is a way a wrong answer destroys a customer: a panel that
  has not flipped `active` yet, an approved renewal not provisioned yet, a wrong
  panel URL, a clock edge.
- **Test:** `tests/unit/test_service_cleanup_decide.py` (a table, every branch),
  `tests/e2e/test_lifecycle_deletion.py`.

## After the adversarial review of the first version (all red on 66ff2aa)

- **The limited clock is the later of `limited_since` and the last change** (an order, an
  admin extend or add-volume): a changed service answers `limited_first_seen` and the job
  restarts the clock. The bot clears `limited_since` and `delete_requested_at` whenever it
  changes a service (provisioning finalisation, `service_ops`, revive), and every
  `limited_since` when deleting goes from off to rehearsal/on.
- **Saving a deletion number while deleting is `on` puts it back to `rehearsal`** and voids the
  rehearsal. **Test:** `test_b2_*` (unit and e2e).
- **An intent is cleared, never finished, when it is older than 48 hours or in a rehearsal**;
  it is finished only in `on`, while the panel answers for other users. Provisioning meeting an
  intent younger than 10 minutes fails the retryable way (`panel_transient`, order stays paid);
  an older one is cleared and committed before its panel call. After the intent is committed
  the facts are loaded once more and an open order or a recent change aborts the delete.
- **A warning is proof only if it was delivered** (`delivered_at`: handed to Telegram, or
  refused by the recipient: blocked, deactivated, chat not found) **and sent after the current
  spell began** (`warned_at >= ended_at`). A generic `TelegramBadRequest` is OUR bug: the claim
  is released, one errors-topic alert goes out, and it does not count. A claim with no delivery
  mark older than 10 minutes is sent again.
- **Warnings have their own stop** (same thresholds as deleting): none is sent, the owner is
  told once, and the list («would warn») waits; approving it sends them. Each warning reloads
  the facts and the exemption right before it is sent.
- **Revive only for the same owner**; another customer's deleted name is taken and a new one is
  rolled. The deleted-notice key carries `deleted_at`, so a second deletion is announced again.
- **A rehearsal records `last_rehearsal_at` only if a panel read succeeded and fewer than half
  failed**; the report counts the failures.
- **A trial an admin extended or gave volume is no longer a trial** (`is_trial_by_refs(...,
  admin_touched=True)`). **A name that answers with another panel id is KEEP** (`panel_id_mismatch`,
  reported). A second process that already deleted it is not counted or announced.
- **Left on the allowlist on purpose** (`tests/unit/test_soft_deleted_accounts.py`):
  `_load_account_for_order` (an ACTIVE order's own account must resolve for idempotent resume),
  the two trial resumes (a deleted account there fails closed on the panel's 404).
- **Tests:** `tests/e2e/test_lifecycle_review.py`, `tests/unit/test_service_cleanup_decide.py`.

## The run (`services/lifecycle_run.py`)

- **Rule: delete in two phases, and re-read first.** Re-read the panel user,
  reload the facts AFTER that read, `decide` again; only if still DELETE commit
  `delete_requested_at`, THEN call `delete_user`, then soft-delete (`deleted_at`)
  and queue the customer's notice in one transaction. Never the panel call before
  the committed intent.
  **Why:** a crash after the panel's delete and before our commit left a live row
  for a user that is gone; the intent plus a 404 lets the next run finish it.
  **Test:** `test_the_intent_is_committed_before_the_panel_is_asked_to_delete`,
  `test_a_crash_between_the_panel_delete_and_our_commit_converges_on_the_next_run`.
- **A 404 never deletes by itself.** A wrong panel address answers 404 for every
  user. Only a service with a committed intent, while the panel still answers for
  at least one other user, is finished. A panel error deletes and marks nothing.
- **Hard stop**: more than 20 deletions in one run, or more than 25% of live
  services when that is more than 5: delete NOTHING, store the list as a batch,
  alert once. **Test:** `tests/e2e/test_lifecycle_batch.py`.
- **A list the owner approves** (`approve_batch`): owner only, mode `on`, valid
  (under 48 h, same deletion numbers), marked used BEFORE the first intent (a
  second confirm deletes nothing), each id re-read and re-decided with the same
  guards (warn-first included), the hard stop not applied.
- **Mode off reads and writes nothing**; an idle run is read-only
  (`tests/integration/test_idle_jobs_read_only.py`). No Telegram send and no panel
  call inside a transaction; a customer message is claimed in `service_notices`,
  committed, then sent (a crash loses a notice, never repeats one).
- **A soft-deleted account is not a service**: every loader goes through
  `services/accounts.py::live_account_clause`; `tests/unit/test_soft_deleted_accounts.py`
  scans for a new `select(PasarGuardAccount…)` without it. Orders, payments and
  the wallet ledger are never touched. The unique index on `panel_username` still
  holds the name; a purchase that re-creates it revives the row.
