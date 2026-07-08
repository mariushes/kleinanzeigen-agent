# Kleinanzeigen Van-Buying Agent — Technical Plan

## Context

The user is searching for a used van (currently a VW T5) on kleinanzeigen.de but lacks car expertise: they can't judge which engine/trim configurations are reliable, what mileage is acceptable for which model, whether a price is fair, or whether a listing's description hides risks. This tool automates that judgment: given a Kleinanzeigen search URL, it scrapes a bounded number of listings, analyzes each one with an LLM against a growing reliability knowledge base (built from car forums and Reddit), and produces a per-listing verdict — price fairness, model/config reliability, and condition red flags — in a local web UI.

Decisions from discovery interview:
- **Users**: personal tool now, architecture leaves room for multi-user later (no auth in MVP)
- **Scope**: code is brand-agnostic from day one; knowledge data seeded for VW T5 only, extended iteratively
- **Platform**: local web app, Python
- **LLM**: Gemini API (free tier) first, behind a provider abstraction so other models can be added later
- **Input**: paste a search URL + max-listings count (resource control)
- **Persistence**: SQLite; **Deployment**: local machine only
- **Pricing**: Kleinanzeigen comparables in local DB are the primary signal; forum-mentioned prices also feed the price model as a secondary signal
- **Forum access**: no Reddit API key — public read-only endpoints (reddit.com `.json`) and HTML scraping of German car forums
- **Kleinanzeigen scraping**: not hand-rolled — vendored as a git submodule ([DanielWTE/ebay-kleinanzeigen-api](https://github.com/DanielWTE/ebay-kleinanzeigen-api), MIT, actively maintained) under `vendor/ebay-kleinanzeigen-api`, run as a local sidecar FastAPI+Playwright process. It already handles anti-bot pacing, deleted-ad detection, and pagination; our app is a thin HTTP client on top plus parsing of its German-language `details` dict into `Listing` fields. This was a deliberate scope-reduction: don't spend implementation effort re-solving scraping that a maintained project already solves well.

Deferred (not in MVP, but architecture must not block them): continuous background monitoring + alerts, multi-user auth, additional LLM providers, other marketplaces beyond Kleinanzeigen, semantic/embedding-based retrieval over `knowledge_entries` (start with tiered exact-match fallback; revisit if the KB grows enough that this visibly isn't sufficient).

### Post-MVP roadmap (user-requested, planned after the MVP checklist)

1. **Buyer-criteria profile → analysis input**: the user can specify special needs (free text and/or structured flags) that the analysis must consider. The immediate personal use case is **camper suitability**: for each listing, judge whether the van (a) already has a camper interior, (b) is suitable/easily convertible, or (c) is a poor camper base — and, for existing conversions, assess the build quality of custom/self-made interiors (professional vs. amateur work, materials, insulation, electrics, gas installation red flags). This becomes an additional scored dimension that feeds the overall verdict. Design note: implement as a general "buyer criteria" mechanism (stored per user/profile, injected into the condition/verdict prompts and scoring) rather than hardcoding camper logic — camper is just the first criteria set.
2. **Chat agent interface**: a conversational view where the user can interrogate a verdict ("why caution?", "compare this to listing X", "what should I ask the seller?"), and the agent can reason over the stored analyses, pull additional details from the web on demand, and query/extend the knowledge base (which is itself populated from web requests). Builds on the existing `LLMProvider` + KB retrieval; needs tool-calling support in the provider abstraction.

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best ecosystem for scraping + LLM orchestration |
| Package/env | `uv` | Fast, reproducible, single-tool workflow |
| Web framework | FastAPI + Jinja2 templates + htmx | Local web app with server-rendered pages; htmx gives interactivity (trigger scrape, poll job progress) without a JS build toolchain |
| DB | SQLite via SQLAlchemy 2.0 + Alembic | Zero setup; SQLAlchemy gives a clean upgrade path to Postgres if multi-user happens |
| Kleinanzeigen access | HTTP client against the vendored `ebay-kleinanzeigen-api` sidecar (`vendor/ebay-kleinanzeigen-api`, git submodule) | Sidecar already does Playwright-based scraping + anti-bot pacing; we don't reimplement it |
| Forum/Reddit scraping | `httpx` + `selectolax` | Only forums/Reddit need hand-rolled HTML parsing (Milestone E); no Playwright needed there — public JSON endpoints and static HTML |
| LLM | `google-genai` SDK (Gemini Developer API, free tier) behind a small `LLMProvider` protocol | Gemini now, swappable later; `response_schema` gives Pydantic-constrained JSON output at no cost |
| Schemas | Pydantic v2 | Structured LLM outputs, config, API models |
| Tests | pytest | Fixtures with saved HTML pages so scraper tests run offline |

Models per task (tunable in config): `gemini-3.1-flash-lite` for listing parsing/normalization (cheap, high volume, higher free-tier RPM), `gemini-3-flash-preview` for condition analysis and knowledge extraction (better reasoning, lower free-tier RPM — ~10 req/min). The free tier is rate-limited per project, not billed, so `GeminiProvider` throttles client-side (`llm_min_call_interval_seconds`, default 6.5s) instead of retrying 429s and burning the daily quota.

> **Current dev-time default**: `llm_model_quality` in `app/config.py` is temporarily set to `gemini-3.1-flash-lite` too (not `gemini-3-flash-preview`) — the flash-preview free-tier daily quota is small and got mostly used up by early smoke tests. Switch it back (or pass `model=` explicitly for a one-off real run) once quota resets or a paid tier is in place; quality-sensitive prompts (condition, pricing) were validated against the real flash-preview model earlier and the outputs were notably good, so this is worth reverting for anything beyond dev iteration.

## Architecture Outline

```
kleinanzeigen-agent/
├── app/
│   ├── main.py                # FastAPI app, routes
│   ├── config.py              # Pydantic settings (API keys, model choices, rate limits)
│   ├── db/
│   │   ├── models.py          # SQLAlchemy models
│   │   └── session.py
│   ├── scraping/
│   │   ├── kleinanzeigen.py   # KleinanzeigenClient: HTTP client for the vendored sidecar + German-attribute parsing → ParsedListing
│   │   └── ingest.py          # search → wanted-ad filtering → detail fetch → upsert Listing rows
│   ├── llm/
│   │   ├── provider.py        # LLMProvider protocol + LLMCallResult (structured-output call)
│   │   ├── gemini.py          # Gemini implementation (google-genai), client-side free-tier throttling
│   │   ├── logging.py         # record_llm_call: persists LLMCallResult into llm_calls
│   │   └── prompts/           # one module per prompt, versioned
│   ├── vehicles/
│   │   └── identity.py        # canonical vehicle identity extraction (LLM-assisted)
│   ├── knowledge/
│   │   ├── sources/           # reddit.py (public .json), forums.py (HTML)
│   │   ├── builder.py         # capped knowledge-collection job
│   │   └── retrieval.py       # KB summary for a given vehicle identity
│   ├── analysis/
│   │   ├── comparables.py     # similarity retrieval: nearest-N by identity/mileage/year/power, with relaxed-match fallback
│   │   ├── pricing.py         # builds the qualitative price-comparison prompt from target + comparables (DB + forum), parses LLM verdict
│   │   ├── condition.py       # LLM condition/red-flag analysis
│   │   └── verdict.py         # combine into overall verdict + confidence
│   ├── jobs.py                # in-process background job runner (asyncio task + status table)
│   └── web/
│       ├── templates/         # Jinja2: dashboard, listing detail, knowledge admin
│       └── static/
├── tests/
│   └── fixtures/              # saved HTML pages, sample LLM outputs
├── PLAN.md                    # this plan, copied into the repo
├── CLAUDE.md                  # project conventions (created at scaffold time)
└── pyproject.toml
```

### Core pipeline (per search run)

1. **Scrape**: `KleinanzeigenClient.search_by_url` calls the sidecar's `POST /inserate-by-url` with `max_pages = ceil(max_listings/25)` → trim to the user's max count. Titles/descriptions are heuristically checked to skip "wanted"/buy-any-car ads mixed into results (`ingest.is_likely_wanted_ad`). For each remaining listing, `KleinanzeigenClient.get_detail` calls the sidecar's `GET /inserat/{id}` and parses its German-language `details` dict (Kilometerstand, Erstzulassung, Leistung, Kraftstoffart, Getriebe, Fahrzeugzustand, …) into price/year/mileage plus an `attributes` JSON blob → upserted as `Listing` rows keyed by `kleinanzeigen_id`.
2. **Identify**: LLM call (`gemini-3.1-flash-lite`) maps title + attributes → canonical `VehicleIdentity` (brand, model, generation, engine code/displacement/power, trim). Stored and reused; identities are the join key to the knowledge base. Brand-agnostic: nothing model-specific in code.
3. **Analyze** (per listing, one `gemini-3-flash-preview` call + local statistics):
   - **Condition**: LLM reads the description + attributes against a red-flag taxonomy (accident, rust, missing service history, short-term resale, vague wording, TÜV expired, "Bastlerfahrzeug", export-price signals…) → structured findings with severity.
   - **Price**: vehicle condition is too heterogeneous for pure statistics to mean much (a low-mileage van with rust and no service history isn't comparable to a high-mileage one that's meticulously maintained), so pricing is **retrieval + LLM qualitative judgment**, not a percentile calculation. Retrieve the top N (~5–8) most similar listings from the DB by vehicle identity + mileage/year/power distance (relaxing identity match — e.g. same model/generation but different engine — if too few close matches exist, flagged as such), plus any forum-sourced price points for the same identity. Pass the target listing (attributes + condition-analysis summary from the previous step) and each comparable — annotated with deltas like "+15,000 km, 1 year older, similar condition, no major red flags" rather than raw numbers — to the LLM and ask it to reason about whether the asking price is fair *given the specific differences*, citing which comparables drove the judgment. Output: price verdict tier (underpriced/fair/overpriced/insufficient_data), an estimated fair range, reasoning text citing specific comparables, and confidence (low if <3 decent comparables exist or none share the same engine/trim).
   - **Reliability**: retrieve KB entries via **tiered fallback matching** — exact identity (same engine/trim) → same brand+model+generation → same brand+model — stopping at the first tier with hits, and recording which tier matched so confidence stays honest. This exists because identity extraction can fragment near-duplicate configs (observed live: "2.0 TDI 179 PS (CFCA)" vs "2.0 TDI 180 PS (CFCA biturbo)" from two ads rounding horsepower differently) — exact-only matching would make one ad's KB invisible to the other. If no tier has coverage → verdict explicitly says "no knowledge yet" and offers a one-click knowledge-collection run for that model.
   - **Verdict**: weighted combination → overall score (e.g. 0–100), recommendation tier (buy-candidate / caution / avoid / insufficient data), and a plain-language reasoning summary.
4. **Present**: dashboard lists analyzed listings sortable by score/price/mileage; detail page shows full breakdown with sources cited (which forum threads informed the reliability verdict).

### Knowledge builder (separate, capped, on-demand job)

- Input: a vehicle identity (e.g. "VW T5 2.5 TDI") + budget caps (max research queries, max LLM calls).
- **Primary source: Gemini google_search grounding** (`WebSearchSource`, `app/knowledge/sources/web_search.py`) — one grounded call per research query returns a synthesized answer plus real web citations (van forums, buying guides). This replaced the originally planned anonymous scraping after live probing showed every free alternative is gated: Reddit JSON returns 403 without an OAuth app, DuckDuckGo HTML anti-bot-challenges scripts, motor-talk.de renders search results client-side, and grounding has free-tier quota **only on `gemini-2.5-flash`** (`llm_model_grounded`) — 3.x models 429 grounded calls regardless of remaining quota.
- Dormant sources behind the same `KnowledgeSource` protocol: `RedditSource` (PRAW read-only; activates by setting REDDIT_CLIENT_ID/SECRET from a free reddit.com/prefs/apps registration), motor-talk.de scraper (future, needs Playwright).
- Extraction: LLM turns each research document into typed `KnowledgeEntry` records: `common_problem` (component, symptom, affected engines/years, cost hints), `mileage_expectation`, `config_advice` (good/bad variants), `price_point` (price + mileage + year mentioned in the wild). Tools and `response_schema` can't be combined in one Gemini call, so research (grounded, free-form) and extraction (structured, flash-lite) are always two separate calls.
- Dedup/merge: entries keyed by (identity, type, component); repeated mentions raise a confidence counter instead of duplicating.
- Progressive & bilingual: each run consumes research angles from `RESEARCH_ANGLES` not yet covered for the identity (tracked in `knowledge_research_runs`) and injects already-known components into the query, so a repeat "Refresh" broadens coverage rather than re-confirming known facts. Queries explicitly request German+English sources (German forums like motor-talk.de carry the richest European-model discussion, and grounding can read them).
- Iterative: rerunning with a higher cap extends coverage. First seed run: VW T5 with a small cap to validate the pipeline before spending more quota.

### Token-budget controls

- Per-listing analysis = 1 `gemini-3.1-flash-lite` call (identity, cached after first time) + 1 `gemini-3-flash-preview` call (condition+verdict, with a pre-condensed KB summary injected, not raw forum text).
- Knowledge building runs only when explicitly triggered, with hard caps in config.
- A `llm_calls` log table records every call's model, purpose, and token counts so spend is visible in the UI.
- Free tier is rate-limited (not billed) per project — as low as 10 requests/minute on `gemini-3-flash-preview`, 1500/day. `GeminiProvider` throttles client-side to stay under this rather than retrying 429s; a 10-listing batch (~20 calls total) takes a couple of minutes purely from this pacing, which is expected and fine for a personal tool.

## Data Model (SQLite)

- `vehicle_identities` — id, brand, model, generation, engine_code, displacement, power_kw, fuel, trim, canonical_label (unique)
- `listings` — id, kleinanzeigen_id (unique), url, title, price_eur, year, mileage_km, attributes JSON, description_text, location, seller_type, image_urls JSON, identity_id FK, first_seen_at, last_seen_at, status (active/removed)
- `analyses` — id, listing_id FK, condition JSON (red flags + severities), price JSON (verdict tier, fair range, comparable_listing_ids used + their deltas, forum_price_points cited, LLM reasoning text, confidence), reliability JSON (KB entries applied, coverage level), overall_score, tier, reasoning_text, llm_model, created_at (append-only: re-analysis creates a new row)
- `knowledge_entries` — id, identity_id FK (nullable model-level vs engine-level scope), entry_type, structured payload JSON, source_url, source_quote, mention_count, confidence, created_at, updated_at
- `search_runs` — id, search_url, max_listings, status, counts, started_at, finished_at (also serves as job-status table for htmx polling)
- `llm_calls` — id, purpose, model, input_tokens, output_tokens, related_entity, created_at

## Risks & Mitigations

1. **Kleinanzeigen blocks scraping** (most likely failure point): delegated to the vendored sidecar, which already handles Playwright-based anti-bot pacing, inter-page delays, and deleted-ad detection — de-risked by reuse rather than by building it ourselves. Our own `KleinanzeigenClient` only needs to handle the sidecar being unreachable (clear `KleinanzeigenApiError`) and 404s for deleted/expired listings.
2. **Cold-start / non-comparable pricing**: since condition varies too much for statistics to be meaningful, pricing leans on LLM qualitative reasoning over the closest available comparables (DB + forum), not on hitting a sample-size threshold; the relaxed-match fallback (broaden from exact engine/trim to model/generation) keeps this useful even with a small DB, and confidence is always surfaced — the UI never shows a price verdict without stating how many/how close the comparables were.
3. **Forum HTML fragility**: each source is an isolated parser with offline HTML fixtures; a broken source degrades coverage, never crashes a run.
4. **LLM hallucination in reliability claims**: every KB entry stores its source URL + quote; the verdict UI cites sources so claims are checkable.

## Implementation Checklist (ordered, small verifiable steps)

**Milestone A — Skeleton & scraping (de-risk first)** — ✅ done
1. ✅ Scaffold project: `uv init`, pyproject, FastAPI hello-world, SQLite + SQLAlchemy + Alembic wired, `config.py` with `.env` loading, CLAUDE.md. Verified: `uv run pytest` green, app serves a page.
2. ✅ Copy this plan into repo as `PLAN.md`.
3. ✅ Vendored `ebay-kleinanzeigen-api` as a git submodule (`vendor/ebay-kleinanzeigen-api`); `KleinanzeigenClient.search_by_url` wraps its `POST /inserate-by-url`. Verified live against a real VW T5 search URL.
4. ✅ `KleinanzeigenClient.get_detail` + `parse_listing_detail` wraps `GET /inserat/{id}` and maps the German `details` dict into `ParsedListing`; `ingest.run_search` filters wanted-ads and upserts `Listing` rows by `kleinanzeigen_id` (create or update on rerun). Verified live: 5 real T5 listings ingested with correct mileage/year/fuel/power parsed.
5. ~~Rate limiting + retry + Fetcher protocol~~ — not needed, the sidecar owns this; our client only wraps sidecar-unreachable and 404-deleted cases.

**Milestone B — LLM foundation & identity** — step 6 ✅ done
6. ✅ `LLMProvider` protocol + `GeminiProvider` (google-genai, free tier) with structured (`response_schema`) outputs, client-side RPM throttling, and `record_llm_call` logging helper. Verified: 3 unit tests with a faked `genai.Client`; two real smoke calls against the live API (`gemini-3.1-flash-lite` and `gemini-3-flash-preview`) with correct parsed output and token usage.
7. Vehicle identity extraction: prompt + normalization + dedup into `vehicle_identities`. ✓ verify: 10 scraped listings map to sensible canonical identities, rerun reuses cached identities.

**Milestone C — Analysis pipeline** — ✅ done
8. ✅ Condition analysis: red-flag taxonomy + prompt → structured findings (`app/analysis/condition.py`). Verified live on 6 real listings incl. a "zum schlachten" (for parts) ad and a "Beschädigtes Fahrzeug"-flagged camper — findings correctly caught the parts-car framing, a description/attribute contradiction, a known T5 180PS BiTDI EGR-cooler oil-consumption risk, and an implausible future-dated repair.
9. ✅ Comparables retrieval (`app/analysis/comparables.py`): nearest-N query, tiered exact_identity → same_generation → same_model fallback, sorted by mileage/year/power distance within a tier. Verified: 6 unit tests on synthetic listings (all three tiers, price-exclusion, target-count cap) + live run over 15 real identified T5 listings showing all three tiers firing correctly.
9b. ✅ Price analysis (qualitative, `app/analysis/pricing.py`): builds the comparison prompt (target + condition summary + annotated deltas per comparable + optional forum price points) → LLM verdict tier + fair range + reasoning + confidence; skips the LLM call entirely (returns `insufficient_data`) when there's nothing to compare against. Verified: 3 unit tests + live run — reasoning correctly cited the specific exact-match comparable and factored in the condition-analysis caveats (high mileage, replacement-engine history) rather than just echoing the comparable's price.
10. ✅ Verdict combiner (`app/analysis/verdict.py`): deterministic (not a 4th LLM call) — price-tier base score adjusted by condition-finding severities and positive signals, tier thresholds, confidence floored by the weaker of price-confidence/reliability-coverage. `run_full_analysis` orchestrates identity→condition→comparables→price→reliability→persist as one `Analysis` row. Verified: 6 unit tests (pure `combine_verdict` + orchestrator with a fake provider) + live 3-listing run — scores sensibly differentiated a clean dealer listing (70, buy_candidate) from a risky engine-swap van (19, avoid) and a comparable-priced high-mileage one (51, caution, high confidence from an exact-identity comparable).

**Milestone D — Web UI** — ✅ done
11. ✅ Search-run flow: form (URL + max count, capped) → `SearchRun` row + FastAPI BackgroundTasks job (`app/jobs.py`) → htmx-polled progress fragment (scraped/analyzed counters, HX-Refresh on completion) → dashboard table sorted by score/price/mileage with tier badges. Verified: driven live in headless Chromium — form submit, progress updates through all stages, 3 analyzed rows, all sort orders, zero console errors. (Browser-driving caught a real bug: the status fragment read `run` while the dashboard passed `active_run` — fixed.)
12. ✅ Listing detail page (`/listings/{id}`): verdict + score with combined reasoning, condition red-flag table (severity/category/quote) + positive signals, price verdict with fair range/confidence/full reasoning, reliability-knowledge section with KB citations (or explicit no-coverage note + identity label), collapsible original description, link to the original ad. Verified: 3 route tests + live browser screenshot review.

**Milestone E — Knowledge base**
13. ✅ `KnowledgeSource` protocol (`ResearchDocument` + citations) with `WebSearchSource` (Gemini grounding, active) and `RedditSource` (PRAW, dormant until credentials). `grounded_completion` added to the `LLMProvider` protocol; 503-retry with backoff in `GeminiProvider`. Verified: 8 offline tests + live grounded research run for "VW T5 2.0 TDI 180 PS CFCA" returning detailed EGR-cooler failure analysis with 16 real citations (t6forum.com, bitdi.eu, honestjohn.co.uk, …).
14. ✅ Knowledge extraction (`app/knowledge/extraction.py`) → typed `KnowledgeEntry` rows; capped builder (`app/knowledge/builder.py`) with in-Python (identity, type, component) dedup that bumps `mention_count`/confidence on repeats and stores a readable `source_label` from grounding citations. Verified: 5 offline tests + live seed run for the T5 CFCA identity → 9 entries created, 3 merged, real facts (EGR cooler, turbo, DSG failure, "avoid 180 PS biturbo / prefer 140 PS", repair-cost price point).
15. ✅ KB wired into analysis: tiered fallback retrieval (exact identity → generation → model) in `retrieval.py`, consumed by `verdict.py` (feeds confidence + reasoning citations) and rendered on the listing detail page with component/detail/mentions/source columns and the matched tier. (Built ahead in Milestone C against the empty table; now populated by step 14.) ✓ verify: browser flow below.
16. ~~motor-talk.de forum source~~ — deferred: motor-talk renders search results client-side (needs Playwright), and `WebSearchSource` grounding already draws on motor-talk + t6forum + buying-guide content via citations. Revisit if grounding coverage proves thin.
17. ✅ Knowledge admin page (`/knowledge`): coverage per identity (listing + entry counts), one-click background collection (Collect/Refresh), cumulative LLM token-spend view, and a per-model KB browse page (`/knowledge/{identity_id}`) listing every entry with type/component/detail/quote/mentions/confidence/source + covered research angles. (Entry *deletion* not built — not needed yet; dedup/merge keeps the KB clean.) ✓ verify: 5 route tests + live browser flow (trigger collect → entries appear → browse full KB per model → show on detail page).

**Milestone F — Polish**
18. ✅ README with setup/run instructions (sidecar + app + `.env`), config-knobs table, and architecture diagram; removed dead config (`comparables_min_decent`). End-to-end pass on a fresh DB driven in-browser: scan → verdicts → collect knowledge → re-analyze folds it into the score. Also added: live-KB detail page + re-analyze flow so newly collected knowledge surfaces on already-analyzed listings.

### Bonus beyond original plan (built during implementation)
- Re-analyze route + job (`execute_reanalyze`) and a "knowledge is newer" banner, so collecting knowledge after a listing was analyzed isn't a dead end.
- Progressive, bilingual knowledge collection (see Milestone E) — a genuine improvement over the fixed-query sketch.

## Verification (overall)

- Offline: `uv run pytest` — parsers against HTML fixtures, pricing math, verdict combination, LLM providers mocked.
- Live smoke: paste a real kleinanzeigen.de VW T5 search URL with max 10 listings → all 10 get verdicts; at least one knowledge-collection run seeds T5 data; re-analysis shows reliability citations.
- Budget check: `llm_calls` table shows per-run token spend within expected bounds (~2 calls/listing + capped KB runs).
