---
paths:
  - "bot/client.py"
  - "tests/unit/test_report_delivery_numbers.py"
  - "services/report_*.py"
  - "services/reporting.py"
  - "services/broadcast.py"
  - "services/finance.py"
  - "services/nightly_digest.py"
  - "scheduler/jobs/report_flush.py"
  - "scheduler/jobs/broadcast_worker.py"
  - "scheduler/jobs/nightly_digest.py"
---

# Reports and broadcasts

Both are **append-only with a worker**, and both are wired that way for the
same reason: Telegram fails independently of the database.

- **A report is written to `report_outbox` inside the business transaction and
  delivered by a scheduler job afterwards.** Never send from inside a money
  transaction.
- **`event_key` names the transition, not the order.**
- **`services/report_events.py` owns the wording; call sites only enqueue.**
  Redaction happens once, at enqueue.
- **A broadcast keeps one row per recipient**, so an interruption resumes
  rather than re-sending. Obey `TelegramRetryAfter`; record a blocked user
  once and never retry them.
- **A report route is «ready» only after a real message reached it.**
  Configured is not proof.
- **A giveaway is not a sale.** `_report_sale` and the finance windows both
  skip `price_toman <= 0`, so free trials stay out of the sales topic and out
  of «فروش امروز». A reseller's *paid* trial still counts: the rule is whether
  money moved, not whether the word «trial» appears.
- **«در حال انقضا» and the broadcast's `expiring_soon` share one subquery**
  (`services/broadcast.py::expiring_users_exists`). Change the window once.
- The scheduler calls `services/`, never the panel directly.
- **Report-group setup paces its sends and honours `TelegramRetryAfter` once,
  inside one wait budget** (`services/report_group.py::_FloodPacer`); a
  throttled topic rename is a failure, never «deleted, recreate». The single
  «🧪 تست» tap never sleeps. A failure reaches the admin through
  `failure_reason_fa`, never as `str(exc)`.
  **Why:** live, 8 topics and 8 test messages back to back hit «Flood control
  exceeded», the group read «نیمه‌آماده», and the screen showed the English
  error cut mid-word.
  **Test:** `tests/unit/test_report_group_flood.py`.
- **Report-group setup holds no transaction across a Telegram call, and saves
  each topic the moment Telegram creates it** (`_save_settings`: re-read,
  change own keys, commit). A second attach of the same group renames the
  saved topics instead of creating more.
  **Why:** live, the attach read the row, spent 19 s on Telegram, a scheduler
  job committed, and the final `UPDATE report_settings` died with «database is
  locked». Eight new topics were never saved and stayed in the group as
  orphans.
  **Test:** `tests/integration/test_report_group_snapshot.py`.
- **A saved topic is rebuilt only when Telegram says it is gone**
  (`TOPIC_ID_INVALID`). `TOPIC_NOT_MODIFIED`, the answer to a rename that
  changes nothing, means the topic is there; any other failure is reported and
  the saved thread kept.
  **Why:** live, every re-attach of the same group made eight new topics: the
  check renamed each topic to the name it already had, Telegram refused, and
  the refusal was read as «deleted». The test fake accepted the rename.
  **Test:** `tests/unit/test_report_group.py` (the fake now answers like the
  Bot API).
- **A report names a product by the shelf**: `product_title_by_package_slug`
  («group — plan», keyed on `store_package_slug`), else the volume and days
  (`report_events.product_shape_fa`). Never `plan.name`, the dynamic plan row.
  **Why:** the owner read «محصول: volume-10GB-30d-p70000-h0-g6-8» in «فروش».
  **Test:** `tests/e2e/test_live_sale_report_product.py`.


## English numerals in all delivered bot reports

Owner explicitly reaffirmed on2026-11-01 that report numbers must be English,
not only numbers in assistant chat. `bot/client.py` is the single production
client factory; its request middleware normalizes message text/captions and media
captions on both send and edit. This also covers receipts, finance views, backups,
legacy queued reports and future report producers using the shared client. It
copies requests rather than mutating stored bodies, payload keys, callback data,
HTML link attributes or opaque URLs. New bot entry points must use this factory
or register the same delivery policy on a scoped subclass. Numeric replacement
keeps UTF-16 widths so explicit Telegram entity offsets stay valid.

Regression: `tests/unit/test_report_delivery_numbers.py`, e2e real-router
harness using `create_bot`. Never turn this into money conversion or rewrite a
subscription URL to achieve a presentation preference.
