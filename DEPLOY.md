# Deployment

This app is two long-running processes plus a database:

- **app** — FastAPI + the analysis pipeline (this repo). Serves the web UI on `:8080`.
- **sidecar** — the vendored `ebay-kleinanzeigen-api` (Playwright + Chromium) that does
  the actual kleinanzeigen.de scraping. Ships its own Dockerfile under
  `vendor/ebay-kleinanzeigen-api/`. The app is a thin HTTP client against it.
- **db** — Postgres. Local dev can still use SQLite (the `app/config.py` default); the
  containerized/AWS path uses Postgres so state survives container restarts and, later,
  multiple app instances.

The app is configured entirely through environment variables (`app/config.py` is
`pydantic-settings`), so the same image runs locally and on AWS with only env changes:

| Env var | Purpose | Local compose value |
|---|---|---|
| `GEMINI_API_KEY` | LLM + grounding calls (**required**) | from your `.env` |
| `DATABASE_URL` | SQLAlchemy URL | `postgresql+psycopg://kleinanzeigen:kleinanzeigen@db:5432/kleinanzeigen` |
| `KLEINANZEIGEN_API_BASE_URL` | where the sidecar lives | `http://sidecar:8000` |

---

## 1. Run the production-shaped stack locally (do this first)

This is the fastest way to confirm the containers, Postgres migration-on-boot, and
app↔sidecar wiring all work — the same shape you'll deploy to AWS, minus the managed
services.

```sh
git submodule update --init          # pulls vendor/ebay-kleinanzeigen-api
cp .env.example .env                  # then put your real GEMINI_API_KEY in it
docker compose up --build
open http://localhost:8080
```

What happens: Postgres starts and becomes healthy → the sidecar builds (this is the slow
step: it installs Chromium) and becomes healthy → the app starts, its entrypoint runs
`alembic upgrade head` against Postgres, then serves the UI. The app waits on both
healthchecks (`depends_on: condition: service_healthy`), so a slow sidecar build won't
race the app.

**This is the one step I could not verify for you** (no Docker daemon in my environment).
I did verify offline: the app imports cleanly with `psycopg` installed, the
`postgresql+psycopg://` URL resolves to the psycopg driver, and the full Alembic
migration chain applies to head on a fresh database. A live `docker compose up` is the
remaining check.

Teardown / reset the DB: `docker compose down -v` (the `-v` drops the `pgdata` volume).

---

## 2. Choices, and where each one takes you toward production

You don't have to make all of these at once — each axis is independent. The order below
is roughly "cheapest/simplest first, most production-ready last."

### 2a. Compute — where the two containers run

| Option | When | Notes |
|---|---|---|
| **Single EC2 + docker-compose** (chosen) | Simplest thing that runs on AWS | Pull this repo onto one instance, install Docker, `docker compose ... up -d`. Postgres is the compose `db` service on the same box (EBS-backed volume) or RDS. No auto-heal/scale, but it *is* running on AWS and behaves exactly like local. Full runbook in §3. |
| **ECS Fargate** | Later, if you outgrow one box | Two containers (app + sidecar) in **one task** so they share a network namespace and the app reaches the sidecar at `localhost:8000`. No servers to patch. Fronted by an ALB. |
| App Runner / Lambda | ❌ don't | App Runner is single-container (no place for the sidecar); Lambda can't hold a persistent Chromium or run the in-process `BackgroundTasks` jobs. |

> **"Doesn't docker-compose not work on Fargate?"** Half true. The `docker-compose.yml`
> *file* is a local-dev tool — nothing on AWS runs it directly. The old `docker compose up`
> **against an ECS context** (`docker context create ecs`) was removed by Docker in 2023;
> that shortcut is gone. But Fargate itself runs multi-container setups fine — you just
> write an **ECS task definition** instead of a compose file. So compose-vs-Fargate is
> really "keep managing one EC2 box" vs. "let AWS manage the host, translate compose to a
> task def." We chose EC2, so the compose file *is* the deployment.

Sidecar note: it needs meaningful CPU/RAM for Chromium — give the instance at least
1 vCPU / 2 GB (a `t3.small` is the practical floor; `t3.medium` is comfortable).

### 2b. Database

- **Now (this repo):** `DATABASE_URL` → Postgres. Code was already driver-agnostic; the
  only change was adding `psycopg` to dependencies. Timestamps are `DateTime(timezone=True)`
  and all JSON columns are generic `JSON`, so nothing was SQLite-specific.
- **On AWS:** provision **RDS Postgres** (or Aurora Serverless v2 if you want scale-to-low).
  Point `DATABASE_URL` at it. RDS handles backups, failover, and patching — the actual
  "production database" story. Put the app and RDS in the same VPC; open the RDS security
  group only to the app's security group.

### 2c. Secrets

- **Now:** `GEMINI_API_KEY` is a plain env var (fine for a personal tool / first deploy).
- **Toward production:** store it in **AWS Secrets Manager** (or SSM Parameter Store,
  cheaper) and have ECS inject it into the task via `secrets:` — it never appears in the
  task definition or image. Same for the RDS password.

### 2d. Things deliberately left for later (and why they're safe to defer)

- **No auth** — it's a personal tool (per PLAN.md). Before exposing it publicly, put it
  behind an ALB + Cognito, or just don't give it a public listener (VPN / SSH tunnel /
  private ALB). This is the single most important thing to decide before it's reachable
  from the internet, because the app can spend your Gemini quota and scrape on demand.
- **Background jobs are in-process** (`FastAPI BackgroundTasks`). A container restart mid-
  scan loses that run's progress (the `search_runs` row stays non-terminal). Fine for one
  instance; if you move to multiple app instances or want durable jobs, that's when a real
  queue (SQS + a worker) earns its place — not before.
- **Migrations run on every app container start** (entrypoint). Correct and idempotent for
  a single instance. With multiple instances you'd instead run migrations as a one-shot
  ECS task in the deploy pipeline and drop them from the app entrypoint, so N containers
  don't race to migrate.
- **Sidecar scaling / sharing** — one sidecar is fine. It's stateless, so if it becomes a
  bottleneck you can run more and load-balance, but don't until it is.

---

## 3. Deploy to a single EC2 instance (chosen path)

This runs the same two containers you tested locally, plus the `db` container, on one box.
The production override ([docker-compose.prod.yml](docker-compose.prod.yml)) layers on
restart policies, a secret-driven DB password, and — importantly — publishes the app on
`127.0.0.1` only (no auth, so it must not be internet-reachable; you tunnel in over SSH).

### 3.1 Launch the instance

- **AMI:** Amazon Linux 2023 (or Ubuntu 22.04). **Type:** `t3.medium` (2 vCPU / 4 GB) — the
  sidecar's Chromium is the memory driver; `t3.small` works but is tight.
- **Storage:** bump the root EBS volume to ~30 GB (the sidecar image + Postgres data live here).
- **Security group:** inbound **SSH (22) from your IP only**. **No 8080 inbound** — the app
  is bound to localhost and reached via SSH tunnel. Outbound: allow all (needs to reach
  Gemini + kleinanzeigen.de).
- If you later use **RDS** instead of the `db` container, put RDS in the same VPC and open
  its security group to the instance's security group only.

### 3.2 Install Docker + Compose plugin

```sh
# Amazon Linux 2023:
sudo dnf install -y docker git
sudo systemctl enable --now docker          # --now enables + starts; survives reboot
sudo usermod -aG docker ec2-user            # re-login after this so `docker` needs no sudo
# Compose v2 plugin:
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
```

### 3.3 Get the code (with the sidecar submodule)

```sh
git clone <your repo url> kleinanzeigen-agent
cd kleinanzeigen-agent
git submodule update --init          # pulls vendor/ebay-kleinanzeigen-api — REQUIRED
```

### 3.4 Provide secrets via a host env file (never committed)

The prod override reads `GEMINI_API_KEY` and `POSTGRES_PASSWORD` from the environment and
**fails loudly if either is missing** (verified). Simplest: a root-only env file.

```sh
umask 077                            # so the file is created 0600
cat > ~/.kleinanzeigen.env <<'EOF'
GEMINI_API_KEY=AI...your-real-key...
POSTGRES_PASSWORD=$(openssl rand -hex 16)   # or paste your own
# DATABASE_URL=postgresql+psycopg://user:pass@your-rds-host:5432/db   # only if using RDS
EOF
```

> Toward production, move these to **AWS Secrets Manager / SSM Parameter Store** and fetch
> them at boot instead of a file on disk — same values, not sitting in a home directory.

### 3.5 Start it

```sh
set -a; . ~/.kleinanzeigen.env; set +a          # load env into the shell
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps      # all healthy?
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f app   # watch migrations + boot
```

`restart: unless-stopped` + `systemctl enable docker` means the whole stack comes back
after a reboot on its own.

### 3.6 Reach the UI (no public port — tunnel in)

From your laptop:

```sh
ssh -N -L 8080:127.0.0.1:8080 ec2-user@<instance-public-ip>
# then open http://localhost:8080 in your browser
```

### 3.7 Verify (same checks that passed locally)

```sh
# on the instance:
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec db \
  psql -U kleinanzeigen -d kleinanzeigen -c "SELECT version_num FROM alembic_version;"
```

Then run a small scan through the tunnelled UI and confirm rows land in `listings` /
`analyses`, exactly as in the local smoke test.

### 3.8 Data & backups

Postgres data lives in the `pgdata` Docker volume on the instance's EBS disk. `docker
compose down` keeps it; `down -v` wipes it. For real durability either **snapshot the EBS
volume** on a schedule, or move to **RDS** (§3.9) which backs itself up.

### 3.9 RDS Postgres (the current data tier — DONE)

This deployment runs against **RDS Postgres over `sslmode=verify-full` TLS**, with the DB
password in **Secrets Manager**, fetched by the instance's **IAM role**. Instead of the
local `db` container, use the RDS compose override via the deploy script:

```sh
cd ~/kleinanzeigen-agent
./deploy-rds.sh              # add --build after pulling code changes
docker compose -f docker-compose.yml -f docker-compose.rds.yml logs -f app
```

**How the pieces fit** (files: [deploy-rds.sh](deploy-rds.sh),
[docker-compose.rds.yml](docker-compose.rds.yml), [app/config.py](app/config.py)):

- `deploy-rds.sh` fetches the DB password from Secrets Manager (via the instance IAM role),
  downloads the RDS CA bundle to `certs/global-bundle.pem`, and exports the connection as
  **discrete `DB_*` parts** (`DB_HOST`, `DB_PASSWORD` **raw**, `DB_SSLMODE=verify-full`, …).
- `app/config.py` assembles the SQLAlchemy URL from those parts via `URL.create`, which
  percent-encodes the password itself. A full `DATABASE_URL` still wins if set (local
  SQLite / the `db`-container path). **One place owns URL assembly** — no hand-encoding in
  shells, which is what makes special-char RDS passwords safe.
- `docker-compose.rds.yml` drops the `db` service, mounts the CA bundle read-only at
  `/certs/global-bundle.pem`, and **`!reset`s the base file's hardcoded `DATABASE_URL`** so
  the `DB_*` parts win.

**Prerequisites** (all one-time, done):

1. **RDS security group** allows `5432` inbound *from the EC2 security group* (a shared SG
   is not enough — the rule must reference the SG). RDS **not** publicly accessible.
2. **Instance IAM role** with `secretsmanager:GetSecretValue` on the DB secret ARN, attached
   via EC2 → Actions → Security → Modify IAM role (takes effect in seconds, no reboot).
3. `~/.kleinanzeigen.env` holds **only `GEMINI_API_KEY`** now — `DATABASE_URL` and
   `POSTGRES_PASSWORD` were removed (they'd shadow the RDS parts).

**Gotchas hit while wiring this up** (so they don't recur):

- **Compose merges `environment` maps**, so the base file's `DATABASE_URL` survived into the
  RDS deploy and — since config prioritizes it — pointed the app at the dead `db` host.
  Fixed with `DATABASE_URL: !reset null` in the override. (Same class as the ports merge.)
- **`%`-encoded passwords crash Alembic's ConfigParser** (`invalid interpolation syntax`).
  `migrations/env.py` escapes `%`→`%%` when handing the URL to Alembic; SQLAlchemy un-escapes.
- **A restarting container can't be `exec`'d or reliably inspected** — read the *image*'s
  baked env and the compose merge output to find where a stray value comes from.

**Verify it's really on RDS with TLS:**

```sh
docker exec kleinanzeigen-agent-app-1 python -c "
from sqlalchemy import create_engine, text
from app.config import get_settings
with create_engine(get_settings().database_url).connect() as c:
    print('head:', c.execute(text('SELECT version_num FROM alembic_version')).scalar())
    print('ssl :', c.execute(text('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).scalar())
"
```

Expect the alembic head revision and `ssl: True`.

## 4. Later hardening (each independent, do when it hurts)

- ~~**RDS** instead of the `db` container~~ — **done, §3.9** (verify-full TLS).
- ~~**Secrets Manager / SSM** instead of the env file~~ — **done** for the DB password
  (§3.9). `GEMINI_API_KEY` still lives in `~/.kleinanzeigen.env`; move it to Secrets
  Manager the same way if you want zero plaintext secrets.
- **Auth + a real listener** — put an ALB (or Caddy/nginx with basic-auth) in front and
  open 8080, *only after* there's auth. Until then, keep the SSH-tunnel model.
- **Durable jobs** — background jobs are in-process (`BackgroundTasks`); a restart mid-scan
  loses that run's progress. Move to SQS + a worker only if that becomes a real problem.
- **Move to ECS Fargate** — if one box stops being enough; translate the two services to a
  task definition (see §2a).
