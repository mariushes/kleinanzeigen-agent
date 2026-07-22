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
                        ▼                                     buyer criteria (LLM,
             reliability KB  ◀──── grounded web research      if a profile is picked)
             (per identity)        (Gemini google_search)           │
                        │              comparables (DB, w/ deltas)   │
                        └──────────────────────┬─────────────────────┘
                                               ▼
                              holistic judgment (one LLM call): weighs
                              price · condition · reliability · positives
                              (· your criteria)
                              → score + per-axis ratings + reasoning
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
   then **Scan**. Optionally pick what you want the van **for** (e.g. *Camper conversion*)
   — every listing in that scan is then also judged on how well it fits those needs, as an
   extra axis in the verdict. Scraping + analysis runs in the background; progress updates
   live. (Analysis is paced by the Gemini free-tier rate limit, so ~10 listings takes a
   couple of minutes.)
2. The dashboard lists analyzed listings, sortable by score / price / mileage. Click one
   for the full breakdown.
3. On the **Knowledge base** page, click **Collect** for a model to research its
   reliability. Re-running **Refresh** broadens coverage (it advances through new research
   angles rather than repeating). Then **Re-analyze** a listing to fold the new knowledge
   into its verdict. The re-analyze form also lets you switch which buyer criteria the
   listing is judged against; each verdict records the criteria it was judged under.

### Buyer criteria

A criteria profile describes what you want the vehicle *for*, in your own words plus the
specific aspects the analysis should rate. **Camper conversion** ships by default: it
rates conversion status, build quality of self-made interiors, electrics, gas installation
(incl. a valid *Gasprüfung*), insulation/damp, and base-vehicle fit.

Profiles live as YAML in [`app/criteria/profiles/`](app/criteria/profiles/) — edit
`camper.yaml` to change the wording, or drop in a new file for a different use case, then
restart the app (or run `uv run python -m app.criteria.loader`). There is no editor UI yet.

Per-requirement results read **meets / partial / fails / Not stated**. "Not stated" means
the ad simply doesn't mention it — a question for the seller, not a mark against the van;
if an ad is silent on everything, the criteria axis shows grey "No data" rather than a bad
rating. Note that conversion details are often only visible in the **photos**, which aren't
analyzed yet, so expect "Not stated" on interior aspects for ads with thin descriptions.

## Configuration

All in [`app/config.py`](app/config.py) (overridable via `.env`). Notable knobs:

| Setting | Default | Purpose |
|---|---|---|
| `default_max_listings` / `max_listings_hard_cap` | 10 / 50 | scrape caps |
| `llm_model_fast` | `gemini-3.1-flash-lite` | identity + extraction |
| `llm_model_quality` | `gemini-3.1-flash-lite` | condition + holistic verdict (see note) |
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

### Project layout

- `app/web/routes/` — one `APIRouter` per concern (dashboard, listings, runs, knowledge);
  `app/main.py` only assembles them.
- `app/services/` — HTTP/template-agnostic read functions (session in, plain data out).
  Put query/shaping logic here, not in a route, so it can be reused.
- `app/analysis/` — `pipeline.py` orchestrates a listing's analysis (DB + LLM) into one
  `Analysis` row; `verdict.py` is the pure scoring step; `judgment.py` is the single
  holistic LLM verdict call; `condition.py`, `criteria.py`, `comparables.py`,
  `reliability_score.py`, and `pricing.py` prepare its inputs.
- `app/criteria/` — buyer-criteria profiles. The wording lives in `profiles/*.yaml`
  (data, version-controlled); `loader.py` upserts them by slug on startup. Add a criteria
  set by adding a YAML file — never a code branch.
- `app/knowledge/` — the reliability KB: `sources/` (grounded web research behind a
  `KnowledgeSource` protocol), `builder.py`, `extraction.py`, `retrieval.py`.
