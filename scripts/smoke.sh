#!/usr/bin/env bash
# Local/CI smoke: apply migrations (optional DB) + run pytest with dummy env.
set -euo pipefail
cd "$(dirname "$0")/.."

export BOT_TOKEN="${BOT_TOKEN:-test-token}"
export SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-false}"
export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///:memory:}"
export PASARGUARD_BASE_URL="${PASARGUARD_BASE_URL:-https://panel.example.com}"
# API key auth only (empty is fine for unit tests that mock the client)
export PASARGUARD_API_KEY="${PASARGUARD_API_KEY:-}"

if [[ "${SKIP_MIGRATE:-0}" != "1" ]] && [[ "${DATABASE_URL}" != *":memory:"* ]]; then
  echo "==> alembic upgrade head"
  alembic upgrade head
else
  echo "==> skip alembic (in-memory or SKIP_MIGRATE=1)"
fi

echo "==> pytest"
pytest "$@"
echo "Smoke OK"
