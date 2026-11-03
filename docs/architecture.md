# Architecture

PasarGuard owns VPN users and service limits. NEO owns catalog, pricing, orders,
payments, wallet ledger, customer presentation and orchestration.

- `bot/`: Telegram handlers, FSM, keyboards and Persian text.
- `web/`: authenticated administration pages and HTTP endpoints.
- `services/`: shared use cases, pricing, financial transactions and outboxes.
- `database/`: SQLAlchemy models; `alembic/`: schema migrations.
- `api_client/`: PasarGuard HTTP client, against `vendor/pasarguard-openapi.json`.
- `scheduler/`: workers that call services for recovery, delivery and lifecycle jobs.

Handlers call services; services own database/API work. Financial values are
integer toman. Payment approval uses idempotency keys and transactional outboxes.
External delivery happens outside financial transactions. Panel retries read
current state before writing absolute values.

Blu payment matching uses permanently reserved exact invoice amounts and an
authenticated iPhone SMS relay. See [the payment guide](BANK_SMS_BLU.md) for the
trust boundary, deduplication, expiry and manual review behavior.

Private deployment settings, customer exports and operational reports are not
part of this public repository or distribution package.
