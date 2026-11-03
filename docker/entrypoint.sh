#!/bin/sh
# Production entrypoint: run Alembic migrations then start the bot process.
set -e
rm -f /tmp/neo-runtime-health.json

echo "Running database migrations (alembic upgrade head)..."
alembic upgrade head
echo "Migrations complete."

exec "$@"
