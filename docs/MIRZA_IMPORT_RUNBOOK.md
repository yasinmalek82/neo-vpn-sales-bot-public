# Mirza import operation

This is an executable path and a release gate checklist, not authorization for
real customer migration. The owner-approved environment and data scope must be
recorded before running apply. Synthetic permission covers synthetic data only.

## Preflight and review

1. Freeze an immutable source export; retain its original private file and hash.
   Keep all source/policy/review files outside Git and Docker build context.
2. Create a private policy JSON (0600). Required fields: `namespace`, `source_unit`
   (`toman` or `rial`); optional explicit `archive_invoice_ids`,
   `accept_quarantine` (`table:source-key` strings), and `panel_bindings`.
   A binding carries invoice_id, exact panel_user_id/panel_username,
   subscription_url and explicit delete_exempt. It never changes panel values.
3. Resolve each approved archive decision to exact source IDs. Preserve names
   and explicit exemptions; never select rows merely because a count matches.
4. Obtain a current read-only panel inventory in the approved environment.
   Verify every selected binding and owner. Missing/unmatched accounts stay
   explicit review items; preserve the source record and financial history.
5. Reconcile pending receipts/payments/extensions. Quarantine acceptance records
   a decision to preserve them for review, not a statement that money was paid.
   Do not turn unpaid source records into wallet credit or a new VPN service.
6. Check target identity collisions. This importer refuses an existing NEO user
   instead of guessing how two balances/roles should merge. Use a fresh prepared
   target or resolve collisions under a separately reviewed reconciliation.
7. Confirm imported trial-counter semantics, catalog/group/pricing configuration,
   reseller commercial rules and source referral-code compatibility. Do not infer
   a new price, balance, owner or discount from plan shape.

Offline preview (no database or panel connection):

```text
python scripts/migrate_mirza.py /private/source.sql \
  --policy /private/policy.json --output /private/review.json
```

Unknown unit produces an unknown total and blocks apply. Inspect every blocker,
opening total, per-user balance, record disposition and inventory binding. Retain
source, policy, review and their hashes together with the rollback backup.
Changing source or policy changes the confirmation digest and requires review.

After an authorized import, the customer desk's source-history button and the
sales section's Mirza review queue provide read-only paginated access. They show
source status/references, archive/quarantine disposition and exact converted
amounts without replaying a payment. The batch retains its source unit separately
from the wallet. Older batches with missing unit metadata display unknown amounts;
reapplying cannot silently infer it or accept conflicting currency metadata.

## Apply in the authorized environment

- Stop new sales/wallet changes only under the owner's authorized cutover plan.
- Take a complete target backup and demonstrate restore into an isolated target.
- Upgrade the explicitly selected target to the approved schema/release. Do not
  use an implicit `.env` database URL; keep the target URL in a private environment
  variable such as `NEO_MIGRATION_DATABASE_URL`.
- The CLI reads panel connection configuration only for binding verification,
  using target DB overrides or explicitly exported process environment values.
  Its executable entry point disables implicit dotenv before shared imports and
  supplies a dummy Telegram token. It never loads the repository `.env` or starts
  Telegram. `NEO_DISABLE_DOTENV=1` is also available to isolated operator processes;
  ordinary bot startup keeps the existing dotenv behavior when it is absent.

```text
python scripts/migrate_mirza.py /private/source.sql \
  --policy /private/policy.json --apply \
  --source-sha256 SOURCE_SHA256 --confirm-plan-sha256 REVIEW_PLAN_SHA256 \
  --target-url-env NEO_MIGRATION_DATABASE_URL
```

Apply requires all blockers resolved/explicitly accepted and both hashes correct.
It verifies selected panel identities/links with GET before writing. If GET
mints another URL with the same origin/path prefix, it validates the reviewed
link through the configured subscription-info endpoint for the exact numeric ID
and username; it retains that reviewed URL. Revoked/foreign links or origin/path
changes still fail. No arbitrary supplied URL is fetched. Financial
apply is one transaction: namespace, user mappings, opening ledger entries,
account mappings, reports and source history commit together. No historical
sale/payment/referral is replayed. A repeated identical apply returns applied=false.
A changed export/policy cannot reuse the bound namespace.

## Read-only frozen-import verification

Before unfreezing any writer, use the same reviewed source/policy and both hashes:

```text
python scripts/migrate_mirza.py /private/source.sql \
  --policy /private/policy.json --verify \
  --source-sha256 SOURCE_SHA256 --confirm-plan-sha256 REVIEW_PLAN_SHA256 \
  --target-url-env NEO_MIGRATION_DATABASE_URL \
  --output /private/new-verification.json
```

The target is explicit; dotenv/default DB is never used. PostgreSQL uses a
consistent REPEATABLE READ read-only transaction; SQLite requires an existing
file and uses mode=ro plus query_only. No panel/Telegram client is created. Output
is a newly created0600 report; stdout exposes only hashes, totals and issue-code
counts. Per-source issue keys stay in the private report, outside Git.

Verification compares every imported identity, individual balance and ledger sum,
one exact opening-credit key/type/amount/reference, role/ban/trial flags, source
key/payload/hash/disposition/owner, and exact account ID/name/link/exemption. It
also refuses unexpected orders, payments, referrals, ledger entries and account
mappings for imported identities. An unchanged aggregate total cannot hide swapped
wallets or a compensating credit/debit. Missing data is unknown, never a verified0.
No difference is repaired, credited, settled, linked or deleted.

Exit0 means this frozen local mapping matches the reviewed plan; exit3 means a
reconciliation mismatch. Exit2 is an invalid/blocked manifest or missing target;
exit1 is an environment/driver failure. Every nonzero result keeps cutover closed.
This is an immediate pre-unfreeze gate: later legitimate sales/admin changes can
fail it and need their own reconciliation. It does not replace live ownership/
link verification, final source freshness or owner approval of actual cutover.

## Reconcile and rollback

- Compare all source and target user/map/history counts, every source wallet
  opening, ledger sum and current balance, roles/bans/trial eligibility, exact
  account names/IDs/ownership and every archive/quarantine reference.
- Run the same import again; prove no row/balance/panel mutation and no duplicate
  report. Inspect the same schema and hashes rather than counts alone.
- Validate customer, representative and administrator navigation and renewals
  against preserved imported accounts in the authorized environment.
- If pre-commit apply fails, roll back the entire transaction. Never continue on
  the failed session and never manually retry only the credit loop.
- After a committed failed cutover, keep sales frozen and restore the complete
  pre-cutover backup under the approved recovery plan. Do not delete financial
  ledger rows or drop the mapping tables as an undo. Any intervening real
  transaction requires separate reconciliation before a restore can be safe.
