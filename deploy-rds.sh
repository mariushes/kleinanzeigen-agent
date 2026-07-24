#!/usr/bin/env bash
# Deploy the app + sidecar against RDS Postgres. Run ON THE EC2 INSTANCE, from the repo root.
# Fetches BOTH secrets from Secrets Manager (via the instance's IAM role) — the DB password
# and the Gemini API key — downloads the RDS CA bundle for sslmode=verify-full, and brings
# the stack up. Nothing secret is stored on the host's disk.
#
#   ./deploy-rds.sh
#
# Requires on the host: the instance IAM role granting secretsmanager:GetSecretValue on both
# $DB_SECRET_ARN and $GEMINI_SECRET_ARN, and network reachability to RDS:5432.
# For LOCAL runs (no IAM role), set GEMINI_API_KEY in the environment to skip its fetch.
set -euo pipefail

# --- RDS connection facts (edit here if the instance/secret changes) ---
RDS_HOST="kleinanzeigen-agent-database.cdkuci8gs2zb.eu-central-1.rds.amazonaws.com"
RDS_PORT=5432
RDS_USER="postgres"
RDS_DB="postgres"
DB_SECRET_ARN="arn:aws:secretsmanager:eu-central-1:437952802416:secret:rds!db-c930822c-d881-46ea-99ac-7131ca8dc57a-jWJfpp"
GEMINI_SECRET_ARN="arn:aws:secretsmanager:eu-central-1:437952802416:secret:kleinanzeigen-agent-gemini-key-4CO1AM"
CA_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

cd "$(dirname "$0")"

# --- Gemini API key: prefer Secrets Manager; fall back to an already-set env var (local) ---
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "[deploy-rds] fetching Gemini API key from Secrets Manager..."
  export GEMINI_API_KEY="$(aws secretsmanager get-secret-value --secret-id "$GEMINI_SECRET_ARN" \
    --query SecretString --output text | jq -r '.GEMINI_API_KEY')"
fi
[ -n "${GEMINI_API_KEY:-}" ] && [ "$GEMINI_API_KEY" != "null" ] || {
  echo "could not obtain GEMINI_API_KEY (from Secrets Manager or the environment)"; exit 1; }

# --- 1. RDS CA bundle for verify-full (mounted into the app container) ---
mkdir -p certs
if [ ! -s certs/global-bundle.pem ]; then
  echo "[deploy-rds] downloading RDS CA bundle..."
  curl -fsSL "$CA_URL" -o certs/global-bundle.pem
fi

# --- 2. DB password from Secrets Manager (never written to disk) ---
echo "[deploy-rds] fetching DB password from Secrets Manager..."
DB_PASS="$(aws secretsmanager get-secret-value --secret-id "$DB_SECRET_ARN" \
  --query SecretString --output text | jq -r '.password')"
[ -n "$DB_PASS" ] && [ "$DB_PASS" != "null" ] || { echo "could not read .password from secret"; exit 1; }

# --- 3. export DB parts RAW; config.py assembles the URL via SQLAlchemy URL.create, which
#        percent-encodes the password itself. No hand-encoding here (that previously tripped
#        Alembic's configparser on `%`). One place owns URL assembly: app/config.py.
export DB_HOST="$RDS_HOST"
export DB_PORT="$RDS_PORT"
export DB_USER="$RDS_USER"
export DB_NAME="$RDS_DB"
export DB_PASSWORD="$DB_PASS"                 # raw, un-encoded
export DB_SSLMODE="verify-full"
export DB_SSLROOTCERT="/certs/global-bundle.pem"
# Make sure no stale full-URL override shadows the parts.
unset DATABASE_URL
echo "[deploy-rds] DB -> ${DB_USER}:***@${DB_HOST}:${DB_PORT}/${DB_NAME} (sslmode=verify-full)"

# --- 4. bring the stack up against RDS (entrypoint runs alembic upgrade head) ---
docker compose -f docker-compose.yml -f docker-compose.rds.yml up -d "$@"

echo "[deploy-rds] done. Follow migrations/boot with:"
echo "  docker compose -f docker-compose.yml -f docker-compose.rds.yml logs -f app"
