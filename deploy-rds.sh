#!/usr/bin/env bash
# Deploy the app + sidecar against RDS Postgres. Run ON THE EC2 INSTANCE, from the repo root.
# Fetches the DB password from Secrets Manager (via the instance's IAM role), downloads the
# RDS CA bundle for sslmode=verify-full, assembles DATABASE_URL, and brings the stack up.
#
#   ./deploy-rds.sh
#
# Requires on the host: GEMINI_API_KEY in the environment (or ~/.kleinanzeigen.env), the
# instance IAM role granting secretsmanager:GetSecretValue on $SECRET_ARN, and network
# reachability to RDS:5432.
set -euo pipefail

# --- RDS connection facts (edit here if the instance/secret changes) ---
RDS_HOST="kleinanzeigen-agent-database.cdkuci8gs2zb.eu-central-1.rds.amazonaws.com"
RDS_PORT=5432
RDS_USER="postgres"
RDS_DB="postgres"
SECRET_ARN="arn:aws:secretsmanager:eu-central-1:437952802416:secret:rds!db-c930822c-d881-46ea-99ac-7131ca8dc57a-jWJfpp"
CA_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

cd "$(dirname "$0")"

# --- load GEMINI_API_KEY (and anything else) from the env file if present ---
if [ -f ~/.kleinanzeigen.env ]; then
  set -a; . ~/.kleinanzeigen.env; set +a
fi
: "${GEMINI_API_KEY:?set GEMINI_API_KEY (in ~/.kleinanzeigen.env or the environment)}"

# --- 1. RDS CA bundle for verify-full (mounted into the app container) ---
mkdir -p certs
if [ ! -s certs/global-bundle.pem ]; then
  echo "[deploy-rds] downloading RDS CA bundle..."
  curl -fsSL "$CA_URL" -o certs/global-bundle.pem
fi

# --- 2. DB password from Secrets Manager (never written to disk) ---
echo "[deploy-rds] fetching DB password from Secrets Manager..."
DB_PASS="$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" \
  --query SecretString --output text | jq -r '.password')"
[ -n "$DB_PASS" ] && [ "$DB_PASS" != "null" ] || { echo "could not read .password from secret"; exit 1; }

# --- 3. URL-encode the password so special chars don't break the URL ---
DB_PASS_ENC="$(jq -rn --arg p "$DB_PASS" '$p|@uri')"

# --- 4. assemble DATABASE_URL with verify-full + the mounted CA path ---
export DATABASE_URL="postgresql+psycopg://${RDS_USER}:${DB_PASS_ENC}@${RDS_HOST}:${RDS_PORT}/${RDS_DB}?sslmode=verify-full&sslrootcert=/certs/global-bundle.pem"
echo "[deploy-rds] DATABASE_URL -> postgresql+psycopg://${RDS_USER}:***@${RDS_HOST}:${RDS_PORT}/${RDS_DB}?sslmode=verify-full"

# --- 5. bring the stack up against RDS (entrypoint runs alembic upgrade head) ---
docker compose -f docker-compose.yml -f docker-compose.rds.yml up -d "$@"

echo "[deploy-rds] done. Follow migrations/boot with:"
echo "  docker compose -f docker-compose.yml -f docker-compose.rds.yml logs -f app"
