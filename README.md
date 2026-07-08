# Kleinanzeigen Van-Buying Agent

A personal tool for buying a used van on [kleinanzeigen.de](https://www.kleinanzeigen.de)
without knowing much about cars. Paste a search URL, and it scrapes a bounded number of
listings and produces a per-listing verdict:

- **Price fairness** — LLM reasoning over the closest comparable listings (not a naive
  average), with the specific mileage/year/condition differences taken into account.
- **Condition red flags** — parses the German ad description for accident history, rust,
  missing service records, "Bastlerfahrzeug"/export signals, oil issues, and more.
- **Reliability** — known problems, good/bad engine-trim variants, and mileage
  expectations for the *specific* model+engine, drawn from a knowledge base built by
  grounded web research over English and German owner forums, with source citations.

Every verdict surfaces its confidence, and thin-data cases say so rather than faking a
number. See [`PLAN.md`](PLAN.md) for the full design and [`CLAUDE.md`](CLAUDE.md) for
conventions.

> Personal, low-volume use. Be respectful of the sites involved and their terms.

## How it works

```
search URL ──▶ ebay-kleinanzeigen-api sidecar (Playwright) ──▶ Listing rows
                                                                    │
                        ┌───────────────────────────────────────────┤
                        ▼                                            ▼
             vehicle identity (LLM)                       condition analysis (LLM)
                        │                                            │
                        ▼                                            ▼
             reliability KB  ◀──── grounded web research    qualitative price analysis
             (per identity)        (Gemini google_search)   (LLM over comparables)
                        │                                            │
                        └────────────────────┬───────────────────────┘
                                             ▼
                                    combined verdict + score
```

- **Scraping** is delegated to the vendored [`ebay-kleinanzeigen-api`](https://github.com/DanielWTE/ebay-kleinanzeigen-api)
  sidecar (a maintained FastAPI+Playwright service); our app is a thin client.
- **LLM** is Google Gemini on the **free tier** (`google-genai`), behind an
  `LLMProvider` protocol so another provider can be swapped in.
- **Storage** is a local SQLite file via SQLAlchemy + Alembic.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12.

```sh
# 1. Install app dependencies
uv sync

# 2. Set up the scraping sidecar (git submodule)
git submodule update --init
cd vendor/ebay-kleinanzeigen-api && uv sync && uv run playwright install chromium && cd ../..

# 3. Configure the LLM key (free — get one at https://aistudio.google.com)
cp .env.example .env
# then edit .env and set GEMINI_API_KEY=...

# 4. Create the database
uv run alembic upgrade head
```

## Running

Two processes, in separate terminals:

```sh
# Terminal 1 — the scraping sidecar on :8000
cd vendor/ebay-kleinanzeigen-api && uv run uvicorn main:app --port 8000

# Terminal 2 — the app on :8080
uv run uvicorn app.main:app --reload --port 8080
```

Then open <http://127.0.0.1:8080>:

1. Paste a Kleinanzeigen **search URL** (e.g. a VW T5 search) and a max-listings count,
   then **Scan**. Scraping + analysis runs in the background; progress updates live.
   (Analysis is paced by the Gemini free-tier rate limit, so ~10 listings takes a couple
   of minutes.)
2. The dashboard lists analyzed listings, sortable by score / price / mileage. Click one
   for the full breakdown.
3. On the **Knowledge base** page, click **Collect** for a model to research its
   reliability. Re-running **Refresh** broadens coverage (it advances through new research
   angles rather than repeating). Then **Re-analyze** a listing to fold the new knowledge
   into its verdict.

## Configuration

All in [`app/config.py`](app/config.py) (overridable via `.env`). Notable knobs:

| Setting | Default | Purpose |
|---|---|---|
| `default_max_listings` / `max_listings_hard_cap` | 10 / 50 | scrape caps |
| `llm_model_fast` | `gemini-3.1-flash-lite` | identity + extraction |
| `llm_model_quality` | `gemini-3.1-flash-lite` | condition + price (see note) |
| `llm_model_grounded` | `gemini-2.5-flash` | web-search research (only tier with free grounding quota) |
| `llm_min_call_interval_seconds` | 6.5 | client-side throttle for the free-tier RPM cap |
| `knowledge_default_max_queries` | 2 | research angles per collection run |

> The free tier is **rate-limited, not billed**. `llm_model_quality` intentionally points
> at flash-lite during development to conserve the small `gemini-3-flash-preview` daily
> quota; raise it deliberately for a higher-quality run.

## Development

```sh
uv run pytest                 # offline test suite (network/LLM faked)
uv run alembic revision --autogenerate -m "..."   # after model changes
uv run alembic upgrade head
```

Tests are offline-first: scrapers run against fixtures and LLM calls are faked, so the
suite needs neither the sidecar nor an API key. Only manual/smoke runs hit the real
services.
