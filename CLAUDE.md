# Kleinanzeigen Van-Buying Agent

Personal tool: paste a kleinanzeigen.de search URL, it scrapes N listings and produces a
per-listing verdict (price fairness, reliability, condition red flags) using an LLM
against a growing knowledge base built from forums/Reddit. See `PLAN.md` for the full
design and implementation checklist.

## Running things

- Install/sync deps: `uv sync`
- Run the app: `uv run uvicorn app.main:app --reload`
- Run tests: `uv run pytest` (scoped to `tests/` via `pyproject.toml` — the vendored submodule has its own test suite, don't run it from here)
- Migrations: `uv run alembic revision --autogenerate -m "..."` then `uv run alembic upgrade head`
- Copy `.env.example` to `.env` and set `GEMINI_API_KEY` before any live LLM run (free tier — get a key at [aistudio.google.com](https://aistudio.google.com)).

### Kleinanzeigen scraping sidecar (required for any scrape/ingest)

We don't scrape kleinanzeigen.de ourselves. `vendor/ebay-kleinanzeigen-api` (git submodule,
[DanielWTE/ebay-kleinanzeigen-api](https://github.com/DanielWTE/ebay-kleinanzeigen-api)) is a
maintained FastAPI+Playwright service that does it, and `app/scraping/kleinanzeigen.py` is just
an HTTP client against it. It must be running on `http://127.0.0.1:8000` (see
`kleinanzeigen_api_base_url` in `app/config.py`) before calling `app/scraping/ingest.run_search`.

First-time setup:
```sh
git submodule update --init
cd vendor/ebay-kleinanzeigen-api && uv sync && uv run playwright install chromium
```

Run it (in its own terminal, alongside our app):
```sh
cd vendor/ebay-kleinanzeigen-api && uv run uvicorn main:app --port 8000
```

## Conventions

- **Brand-agnostic code, narrow data**: nothing in `app/` should hardcode a specific
  brand/model (e.g. "T5"). Model-specific knowledge lives only in the `knowledge_entries`
  table and is extended by running the knowledge builder, not by adding code branches.
- **LLMProvider/KnowledgeSource are protocols**: LLM calls and forum/Reddit sources are
  each behind a small `Protocol` interface (see `app/llm/provider.py`,
  `app/knowledge/sources/`) so implementations can be swapped without touching callers.
  Kleinanzeigen access itself is not behind a protocol — it's a single client
  (`app/scraping/kleinanzeigen.py`) against the vendored sidecar (see below); there's
  only one implementation and no reason to abstract it further.
- **Every LLM call is logged**: after calling `provider.structured_completion(...)`, pass
  the result to `app/llm/logging.py::record_llm_call` — this is how per-run token usage
  stays visible in the UI.
- **Gemini free tier is rate-limited, not billed**: don't add retry-on-429 logic as the
  primary defense — `GeminiProvider` already throttles client-side to
  `llm_min_call_interval_seconds` (`app/config.py`) to stay under the per-project RPM cap.
  If you add a new call site, it inherits this automatically through the shared provider
  instance; don't bypass it with a raw `genai.Client` call.
- **`llm_model_quality` defaults to flash-lite during development**: `gemini-3-flash-preview`'s
  free-tier daily quota is small and easily exhausted by smoke tests. Don't do
  repeated/exploratory live runs against it — `app/config.py`'s `llm_model_quality`
  currently points at `gemini-3.1-flash-lite` for that reason. Only point it back at
  `gemini-3-flash-preview` (or pass `model=` for a one-off call) for a deliberate,
  small-scale verification run, and say so explicitly first.
- **Knowledge sources are gated; web-search grounding is the only free one**: live
  probing showed Reddit JSON 403s without an OAuth app, DuckDuckGo HTML anti-bot-blocks
  scripts, and motor-talk.de renders results client-side. So the active `KnowledgeSource`
  is `WebSearchSource` (Gemini `google_search` grounding). Grounding has free-tier quota
  **only on `gemini-2.5-flash`** (`llm_model_grounded`); 3.x models 429 grounded calls
  regardless of remaining quota — don't switch the grounded model to a 3.x id.
  `RedditSource` stays wired to the same protocol but dormant until credentials exist.
- **Research and extraction are always two calls**: Gemini rejects combining `tools`
  (search) with `response_schema` (structured JSON) in one call. The grounded research
  call returns free-form text + citations; a separate structured call extracts typed
  `KnowledgeEntry` rows. Don't try to collapse them.
- **Knowledge collection is progressive, not repetitive**: the builder consumes research
  angles from `RESEARCH_ANGLES` that haven't been covered for that identity yet (tracked
  in `knowledge_research_runs`) and passes already-known component names into the query so
  the model hunts new facts. A repeat "Refresh" should broaden coverage — don't revert it
  to a fixed query list. Queries also explicitly request German- and English-language
  sources, since the richest reliability discussion for European models is on German
  forums (motor-talk.de etc.) that grounding can read.
- **Offline-first tests**: scraper and knowledge-source tests run against fakes/fixtures,
  not live network calls. Only manual/smoke runs hit the real sites/APIs.
- **Scraping is capped**: every scrape/knowledge-collection entrypoint takes an explicit
  max-count/cap argument (see `app/config.py` defaults). Never add an uncapped "scrape
  everything" path. Kleinanzeigen-side politeness (delays, anti-bot pacing) is the
  vendored sidecar's job, not ours — don't reimplement it here. Forum/Reddit sources we
  scrape directly (Milestone E) still need their own delays since nothing else provides that.
- **Pricing is qualitative, not statistical**: `app/analysis/pricing.py` asks the LLM to
  reason over the closest retrieved comparables (with annotated deltas) rather than
  computing a percentile/median — vehicle condition varies too much for pure stats to be
  meaningful. Don't reintroduce a stats-only price band.
- **Confidence is always surfaced**: any verdict (price, reliability) that's based on
  thin data (few comparables, no KB coverage) must say so explicitly rather than
  presenting a falsely confident number.
