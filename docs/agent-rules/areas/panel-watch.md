---
paths:
  - "services/panel_watch.py"
  - "scheduler/jobs/panel_watch.py"
  - "database/models/panel_node_state.py"
---

# Panel watch

`scheduler/jobs/panel_watch.py` reads `GET /api/nodes/simple` every
`PANEL_WATCH_INTERVAL_SECONDS` (default 120; **0 turns the job off**, and it
is then not even registered). `services/panel_watch.py::check_panel_and_nodes`
keeps one `panel_node_states` row per node plus **row 0 for the panel API
itself**. The admin home is database-only, so without stored state it could
never warn.

## Statuses

- **Down** = `error`, or `unreachable` (row 0: the simple list could not be
  read, a refused key included; the bot cannot sell while it cannot read).
- **Warning** = `limited`. **Up** = `connected`.
- **`connecting` and `disabled` never alert**: connecting is transient,
  disabled is the owner's own act.
- **Disabling a node we had alerted on clears `reported_status`**, so enabling
  it later is not announced as a recovery from a week-long «outage».

## Debounce

- **Two consecutive checks in the same status before a down or limited
  alert**, so one failed read or one mid-reconnect is silent. A flap (down,
  up, down) restarts the count.
- **error → connecting → error between polls is one outage, not a flap.**
  `connecting` on a down or limited row keeps its status and `status_since`
  and counts the check, so a node that never stays in `error` for two polls
  still alerts.
- **`reported_status` stops the third check from alerting again.**
- **«Recovered» goes out only for a node we alerted on, after two consecutive
  `connected` checks** (a down, connected, down flap sends nothing), with the
  outage measured to the first `connected`.
- **`reported_since` keeps when the unhealthy stretch began**, because
  recovery usually passes through `connecting` and `status_since` alone would
  understate the outage.
- **While the panel is unreachable the node rows are left exactly as they
  were.** Nothing is known about them, and counting a check against them would
  alert on guesses.

- **A pass with no news writes nothing.** A node that is not down or limited,
  with the same status, the same name and its count already at
  `ALERT_AFTER_CHECKS`, is left alone, so `last_checked_at` is the last check
  that recorded something. An outage still counts every check.
  **Why:** a healthy panel still made every pass a commit, and every commit
  fails a handler that read before it and writes after («database is locked»,
  WAL snapshot).
  **Test:** `tests/integration/test_idle_jobs_read_only.py`.

## Alerts

- **Enqueued, never sent** (`report_events.panel_*`, topic «system»), in the
  same transaction as the state change; the caller commits and the outbox
  worker delivers.
- **`event_key` = `panel_node:{node_id}:{status}:{status_since}`**, the
  transition. One node down twice in a day is two alerts and a re-run of the
  same state is none.
- **`status_since` is normalized to UTC before it is formatted.** SQLite
  returns naive datetimes and a key that changes on reload would defeat the
  dedupe.
- **The node's own `message` comes from `GET /api/nodes` and is read only when
  a node is about to alert.** A failed read still sends the alert, without it.
  It is escaped and cut to 80 characters.
- **`api_key` and `server_ca` are never declared on `PanelNode`**, so they
  cannot reach a log, a report or a screen.

## Edge cases

- **A 403 on the node list is a missing permission, not an outage.** The panel
  is then probed with `GET /api/admin`; if that answers, row 0 is `connected`,
  node rows stay untouched, nothing alerts
  (`WatchResult.nodes_readable=False`). 401, network, 5xx, or a failed probe
  stay «unreachable».
- **A node missing from the list is deleted without an alert**: gone is not
  down.
- **The home warns for rows that are down with `checks_in_status >= 2`**
  («⚠️ سرور «…» قطع است (از …)», «⚠️ پنل در دسترس نیست»), the same threshold as
  the alert, so the home and the group never disagree. `limited` alerts but
  does not warn on the home.
- **One pass at a time**: a module lock in the job plus `max_instances=1`. Two
  overlapping passes would each count a check and alert one check early.

## Sale gate while panel is down

- **Rule**: While the panel is confirmed down (`is_panel_down`: watcher enabled,
  row 0 exists in `panel_node_states`, down status with `checks_in_status >= 2`,
  and `last_checked_at` within 3 watcher intervals), refuse every new order,
  renewal, plan switch, custom purchase, order confirmation, wallet payment, and
  trial claim (personal and reseller) with `purchase_paused_panel`. Fail open on
  a single failed check or stale row. Resuming and submitting receipts for
  pre-existing open orders remains open.
  **Why**: A customer who paid and then has to wait costs more trust than a
  customer told to come back later. Ambiguous states fail open so a dead watcher
  or temporary flap never locks customers out.
  **Test**: `tests/unit/test_panel_sale_gate.py`, `tests/e2e/test_panel_sale_gate.py`.
- **One guard, `bot/handlers/sale_gate.py::refuse_if_panel_down`; call sites
  never move their own `callback.answer()` for it.** It sits in `_start_flow`
  (menu, bottom keyboard, restart), `start_renew_purchase`,
  `switch_plan_for_account`, `choose_custom_from_ready`, `confirm_order` (one
  call, after both resume branches, whatever the price or wallet split),
  `render_trial` and `_render_reseller_trial`. Where the handler already
  answered, pass `callback.message`; the popup is for a handler whose first act
  is the guard. Resuming an open order always runs before the guard.
  **Why:** a guard that costs a handler its `answer()` order is a refactor of a
  money path; a guard split by price (card only, wallet only) lets a zero-price
  order through.
  **Test:** each site has one e2e test that goes red when its call is removed.
