#!/usr/bin/env sh
# Run DB migrations to head before starting the app, so a fresh Postgres (or a
# schema behind the code) is brought up to date on every deploy. Idempotent:
# alembic no-ops when already at head. In a multi-instance setup you'd move this
# to a one-shot migration task instead of every app container — noted in DEPLOY.md.
set -e

echo "[entrypoint] running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
