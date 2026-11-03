---
paths:
  - "services/mirza_*.py"
  - "services/account_linking.py"
  - "services/subscription_identity.py"
  - "database/models/mirza_import.py"
  - "scripts/migrate_mirza.py"
  - "bot/handlers/account_link.py"
  - "bot/keyboards/account_link.py"
  - "bot/handlers/mirza_history.py"
  - "bot/keyboards/mirza_history.py"
  - "tests/integration/test_mirza_import.py"
  - "tests/integration/test_mirza_binding_verification.py"
  - "tests/integration/test_mirza_reconcile.py"
  - "tests/integration/test_account_linking.py"
  - "tests/integration/test_mirza_history.py"
  - "tests/integration/test_mirza_unit_migration.py"
  - "tests/unit/test_mirza_*.py"
  - "tests/unit/test_subscription_identity.py"
  - "tests/e2e/test_manual_account_link.py"
  - "tests/e2e/test_mirza_history.py"
---

# Mirza migration and explicit existing-account linking

- SQL exports are inert input; never execute their statements. Keep the parser's
  migration-field allowlist, omit panel passwords/runtime tokens/subscription
  blobs and keep per-customer preview files private (0600). Never publish them.
- A source namespace binds one export and one exact policy/preview digest.
  Source or decisions changing requires fresh review; a repeated applied snapshot
  is a no-op, never another opening credit.
- Money has an explicit source unit and exact integer conversion. Unknown unit
  means unknown total, not zero. The final wallet balance becomes one shared
  wallet opening entry; historical payments/sales stay source history and never
  replay as top-ups, orders, revenue or referral rewards.
- Persist the reviewed source unit on each batch. Historical batches without
  metadata retain an unknown unit; a repeated apply must reject conflicting
  metadata. Admin history and quarantine views are read-only and cannot settle
  a source payment or mutate a wallet.
- Existing NEO users/balances are refused until a separate reconciliation exists;
  do not silently merge them. Archive only explicit source invoice IDs, never a
  count or an inferred owner. Preserve names exactly and keep `mirza_sample`
  outside customer mappings. All source records stay mapped/history/quarantined.
- Reviewed inventory bindings must match the source name and current panel
  identity. Fetch/verify panel snapshots before the financial write transaction;
  importer apply has no remote IO and no panel writes. Duplicate/foreign panel
  ownership aborts and rolls back the entire apply.
- A panel GET can mint a new signed subscription URL. Accept a token-only
  difference only if the configured panel's subscription-info GET confirms the
  reviewed link's exact numeric ID and username. Keep the reviewed URL; never
  trust a locally decoded token or fetch an arbitrary supplied URL. Revoked
  links and origin/path changes still require fresh review.
- A remaining source trial counter <=0 keeps the legacy direct-trial entitlement
  consumed through `users.legacy_trial_consumed`, without fabricating a date.
  Existing admin reset clears that flag; tunnel/group rules stay shared.
- Manual linking is an explicit admin preview/confirm flow. Re-read both panel
  identity and local ownership after the await (`populate_existing=True` also
  invalidates a cached mapping). Another owner is always refused; same owner is
  a no-op. Newly linked manual/friend accounts are deletion-exempt. No wallet,
  order, usage, expiry, device limit, group or connection mutation is involved.
- Synthetic rehearsal permission does not authorize importing actual customer
  data, including into an isolated database. Prepare the exact manifest and ask
  only for the missing operational scope when independent work is ready.
- Regressions live in the paths above; the versioned operator checklist is in
  `docs/MIRZA_IMPORT_RUNBOOK.md`. Private historical rehearsal reports remain
  local and do not establish readiness of this checkout.
