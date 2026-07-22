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
| **Single EC2 + docker-compose** | Cheapest first cut on AWS | `scp` this repo (or pull it) onto one instance, install Docker, `docker compose up -d`. Postgres can be the compose `db` service on the same box, or RDS. No auto-heal/scale, but it *is* running on AWS and behaves exactly like local. |
| **ECS Fargate** (recommended target) | "Iterating toward production" | Two container definitions (app + sidecar) in **one task** so they share a network namespace and the app reaches the sidecar at `localhost:8000` — or two services with service discovery. No servers to patch. Fronted by an ALB. This is the natural home for this app. |
| App Runner / Lambda | ❌ don't | App Runner is single-container (no place for the sidecar); Lambda can't hold a persistent Chromium or run the in-process `BackgroundTasks` jobs. |

Sidecar note: it needs meaningful CPU/RAM for Chromium — give the task/instance at least
1 vCPU / 2 GB, more if you scrape larger batches.

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

## 3. Suggested graduation path

1. `docker compose up` locally → confirm end-to-end (§1). ← **you are here after this change**
2. Push both images to **ECR**.
3. Stand up **RDS Postgres**; set `DATABASE_URL` to it.
4. Run on **ECS Fargate** (one task, two containers) behind an ALB with **no public
   listener** (or a private one) — no auth yet, so keep it unreachable from the internet.
5. Move `GEMINI_API_KEY` + DB password into **Secrets Manager**.
6. Only if/when needed: split migrations into a one-shot task, add auth, add an SQS-backed
   worker for durable jobs.

Steps 1–3 give you a real, persistent AWS deployment; 4–6 are the incremental hardening.
