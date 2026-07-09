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
- **LLMProvider/KnowledgeSource are protocols**: LLM calls and web/forum research sources
  are each behind a small `Protocol` interface (see `app/llm/provider.py`,
  `app/knowledge/sources/`) so implementations can be swapped without touching callers.
  Kleinanzeigen access itself is not behind a protocol — it's a single client
  (`app/scraping/kleinanzeigen.py`) against the vendored sidecar (see below); there's
  only one implementation and no reason to abstract it further.
- **Web layer is thin; logic lives in services**: routes are one `APIRouter` per concern
  in `app/web/routes/` (`main.py` is app-assembly only), and they stay thin — DB reads go
  through `app/services/{listings,knowledge}.py`, which are HTTP/template-agnostic
  (session in, plain data out). Put query/shaping logic in a service, not a route, so the
  same function can back both a page and (later) a chat tool. Analysis follows the same
  split: `verdict.py` is pure scoring (`build_verdict`, no DB/LLM), `pipeline.py` is the
  DB/LLM orchestration (`run_full_analysis`).
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
  A Reddit source was removed (needed OAuth credentials that don't exist); if you re-add
  one, it goes behind the same `KnowledgeSource` protocol in `app/knowledge/sources/`.
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
- **The verdict is one holistic LLM call, not a formula** (user decision): `judgment.py`
  gets all the evidence at once — the ad's red flags, annotated comparables, KB facts and
  the deterministic reliability read — and returns the 0–100 score, recommendation, and a
  `good/fair/poor` rating + note per axis (price/condition/reliability/positives). Numbers
  come from a judgment over real evidence, not arithmetic on penalties. Don't reintroduce
  a neutral-baseline additive scorer or per-axis penalty constants for the headline score.
- **Score = the model's read; confidence = how much we knew — kept separate.** Confidence
  stays **deterministic** (`verdict.py::_combined_confidence`): floored by the weaker of
  price-data presence and KB match tier, so a fluent verdict over thin data still reads as
  low confidence. Absence of comparables/KB must lower *confidence*, never drag the score
  down — tell the model this in the prompt too.
- **`no_data` is stamped in code, not invented by the LLM** (user decision): the LLM only
  rates `good/fair/poor`; `verdict.py::build_verdict` overrides an axis to `no_data`
  (with `has_data=False`) when the evidence genuinely doesn't exist — no comparables for
  price, no KB coverage for reliability. The UI shows grey "No data" so the user can tell
  a neutral "fair" from a real absence of evidence. Condition/positives always have data
  (they're about this ad's own text).
- **Sub-scores are the product, the overall score is just a sort key** (user decision):
  the UI leads with the four colored axis chips (dashboard) / axis cards (detail page) and
  the holistic reasoning; the summed score is rendered muted. Don't reintroduce a big
  headline score or signed ± contribution columns.
- **Structured condition + KB extraction is still persisted** even though the verdict is
  the holistic call: `condition.py` returns typed findings/positive_signals and the KB
  keeps typed entries so they can be stored, listed, and reused for future price
  comparison and retrieval. `condition.py` findings must stay listing-specific (ad red
  flags only) — model-general reliability belongs on the reliability axis (KB rules +
  holistic call), or it double-counts.
- **The deterministic reliability read feeds the prompt and the evidence UI, not the
  score directly.** `reliability_score.py` (transparent, symmetric rules over structured
  KB fields — severity/onset_km/stance/sentiment, tier-scaled, `strength`/positive
  `overall_assessment` earn bonus so aging models aren't ratcheted to "severe") is passed
  into `judgment.py` as one input and shown as strengths/concerns bullets on the detail
  page. It's evidence for the LLM's reliability rating, no longer a parallel headline
  score — don't resurrect `score_llm_variant` or the dueling two-signal table.
- **New models get a first knowledge pass automatically**: `execute_search_run`
  auto-collects for identities that have never been researched (budgeted via
  `auto_collect_*` settings, fail-soft on grounding errors) so first verdicts aren't
  knowledge-blind. Don't remove the per-run budget — an all-new-models scan must not
  drain the grounded quota.
- **Confidence is always surfaced**: any verdict based on thin data (few comparables, no
  KB coverage) must say so via the confidence label rather than a falsely confident score.
