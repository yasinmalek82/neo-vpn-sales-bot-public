# Task-specific rules

Codex reads the root `AGENTS.md` as project instructions. These reference files
are read explicitly when relevant; their `paths:` metadata is a routing aid,
not a claim that Codex automatically loads Claude rules. Match planned files
against that metadata, including overlapping rules. Read the shared engineering
guide before code changes; do not load every area for an unrelated task.

| Work area | Read before its code |
| --- | --- |
| Customer copy, brand, signatures, usernames | [Copy and brand](areas/copy-and-brand.md) |
| Keyboard layout and button colours | [Keyboards](areas/keyboards.md) |
| Catalog, store edits, undo, plan ladder | [Catalog](areas/catalog.md) |
| Telegram store admin | [Store admin UI](areas/store-admin-ui.md) |
| Reseller prices, trials, labels and renewal | [Resellers](areas/resellers.md) |
| Pricing, promo codes and offers | [Pricing](areas/pricing.md) |
| Provisioning, renewal, devices, plan/group switches | [Provisioning](areas/provisioning.md) |
| Mirza export/import and existing-account linking | [Mirza migration](areas/mirza-migration.md) |
| Free trials | [Trials](areas/trials.md) |
| Report outbox, broadcast, finance and digest | [Reports and broadcasts](areas/reports-and-broadcasts.md) |
| Payment receipt and payment card | [Payments](areas/payments.md) |
| Admin home and customer desk | [Admin UI](areas/admin-ui.md) |
| Panel/node alerts and sale gate | [Panel watch](areas/panel-watch.md) |
| Service QR and delivery | [QR](areas/qr.md) |
| ISP disruption reports | [Disruption](areas/disruption.md) |
| Expiry, volume warnings and ended-service deletion | [Service lifecycle](areas/service-lifecycle.md) |
| Retail marketing campaigns and upgrades | [Marketing](areas/marketing.md) |
| Mini App, web routes, auth and web-backed services | [Web panel](areas/web-panel.md) |

The web panel guide is large. Use its headings to locate the affected screen;
read the shared layers/auth rules and that screen's section, following its
cross-references rather than loading unrelated screens. Its `paths:` list also
includes services used outside `web/`.

## Maintaining instructions

- Root instructions hold only cross-cutting constraints; use area files for
  detailed rules, recorded as rule, why and regression test.
- Keep path metadata valid and this index complete. `tests/unit/test_agent_rules.py`
  checks both, referenced file existence and the root startup size budget.
- `.claude/rules` is a relative symlink here for existing plans and historical
  references; edit only these canonical files. It contains no second copy.

For public documentation, use [the document index](../README.md).
