# NEO — Codex instructions

Persian Telegram VPN sales bot. PasarGuard owns the VPN control plane; this
project owns catalog, prices, orders, payments, wallet and orchestration.

## Start small

- Check `pwd`, branch and `git status --short`; preserve unrelated edits.
  Use the assigned worktree. Never assume this checkout matches a release.
- Read only the plan/handoff supplied for this task. A plan is a spec, not
  evidence of implementation. Stop and report conflicts with project rules.
- Before reading or changing code, use [the area index](docs/agent-rules/README.md)
  to read matching rules, including their `paths:` metadata. For code changes
  also read [engineering guidance](docs/agent-rules/engineering.md).
- Do not bulk-read plans, handoffs, history or archives at session start.
  Old references to `CLAUDE.md` now mean this file plus the relevant guidance;
  `.claude/rules/` is a compatibility alias for `docs/agent-rules/areas/`.
- Use focused `rg` searches and line ranges. Graphify is optional when available
  and useful for named symbols; do not install tools or hooks for discovery.

## Boundaries

- No reading `.env` or `data/bot.db`, package installation, bot start/restart,
  live Telegram/panel operations, migrations, deployment, commit, merge or push
  unless the owner explicitly authorizes that action. Deploy an approved tag.
- Planning-only means documents only. Explain scope before sensitive money,
  provisioning, auth, scheduler or database changes; resolve unclear requirements.
- Keep diffs scoped; reuse shared functions. Do not refactor prior-phase guards.
- Work directly on small tasks. Delegate only when the task calls for it, using
  available Codex capabilities; never require Claude models or agent presets.
- Reply in concise Persian; isolate code/paths for readability. Report evidence,
  open gates and what was not done; distinguish local checks from live readiness.

## Invariants

- Flow: handlers → services → database/API; scheduler → services. Pricing has
  no Telegram/panel imports. Keyboards belong only in `bot/keyboards/`.
- Money is integer toman (`int`/`Decimal`), never `float`. Preserve idempotency
  and `event_key` guards. Panel retries read first and write absolute values.
- Write reports into `report_outbox` in the business transaction; deliver outside
  it. Never send Telegram messages inside a database transaction.
- No async lazy relationships or read snapshots across network awaits; release
  the read transaction, then re-read rows when deciding the write.
- Preserve dispatcher event isolation and FSM input steps; no waiting in handlers.
- Catalog identity is slug. No panel group means no sale. Unlimited devices is
  `0`, never `None` or `x or 1`. Never infer renewal/plan-switch intent from a match.
- PasarGuard endpoints must exist in `docs/vendor/pasarguard-openapi.json` or
  report BLOCKED. Never guess a contract.
- Customer copy: «تو», English numerals in delivered reports; dynamic prices/counts/durations via shared
  brand/format helpers; no raw exceptions or unsupported claims. Admin messages
  carry navigation; button colours use `styled()` with one primary per keyboard.
- Every saved toggle must have a consumer. Never delete, skip, xfail or weaken
  existing tests to hide failures; money/idempotency/delivery fixes need regressions.

## Verify in the reported checkout

Use its existing `.venv`, or activate the main checkout's venv by absolute path
while staying in the assigned worktree. Do not install missing dependencies.

```bash
source .venv/bin/activate
make lint
BOT_TOKEN=test-token SCHEDULER_ENABLED=false python -m pytest -q
alembic heads
```

All checks must pass; Alembic must show one head. Run focused tests while editing
and the full gate once on the final tree; repeat only after relevant changes.
Report environment failures as blockers, never as passes. Verification commands
above do not authorize bot startup, live probes or database upgrades.
