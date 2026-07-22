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

1. ~~**Buyer-criteria profile → analysis input**~~ — ✅ **done, see Milestone J**. Implemented as a general mechanism: criteria sets are YAML data (`app/criteria/profiles/`), camper conversion is the first one, and the fit becomes a fifth axis in the holistic verdict. Still open (deliberately deferred): **image analysis** — conversion quality is usually visible in the photos and rarely described in the ad text, so feeding `Listing.image_urls` into the criteria/condition calls is the natural next step.
2. **Chat agent interface**: a conversational view where the user can interrogate a verdict ("why caution?", "compare this to listing X", "what should I ask the seller?"), and the agent can reason over the stored analyses, pull additional details from the web on demand, and query/extend the knowledge base (which is itself populated from web requests). Builds on the existing `LLMProvider` + KB retrieval; needs tool-calling support in the provider abstraction.
3. **Scope-aware knowledge + KB matching rework** — see "Knowledge-base matching rework" below. Step 1 (identity stability) is done; the scope work is the open part.

### Knowledge-base matching rework (rough plan)

**The problem, observed live.** A listing identified as `Volkswagen | T5 Transporter | 1.9 TDI 102 PS` had no knowledge of its own, fell back to the `same_model` tier, and inherited **17 entries from the 2.0 TDI** — including `EGR cooler`, `DPF`, `fuel high-pressure pump` and `2.0 BiTDI 180 PS (CFCA)` config advice. The 1.9 TDI is a different engine family; those are not its faults. The verdict and the evidence table presented them as if they were.

Two root causes, only one of which is about matching:

- **Identity fragmentation** (fixed, Step 1): the LLM split the same vehicle inconsistently across `model`/`generation` — one ad became `Volkswagen | Transporter | T5` (generation="T5"), another `Volkswagen | T5 Transporter | …` (model="T5 Transporter"). These couldn't match each other even at the `same_model` tier, which compares `model` for equality.
- **No scope on knowledge entries** (open): `KnowledgeEntry` records *which identity it was collected under* and nothing else. Nothing marks a fact as engine-specific (`EGR cooler`, `DPF`, `HPFP`) versus body/model-general (`sliding door`, `driveshaft splines`, `water pump`). So a fallback match is all-or-nothing: inherit every fact from a sibling variant, or none.

**Is tiered fallback still needed at all?** Mostly not, and this is the key insight (user observation): now that `execute_search_run` *and* `execute_reanalyze` auto-collect for never-researched identities, every identity ends up with its own entries — the fallback rarely fires, and when it does it's actively harmful (the case above). Tiered fallback earns its keep in exactly two situations:

1. **The listing doesn't reveal its engine or generation.** An ad that names no engine can't be researched at engine granularity, so model-wide knowledge is the *correct* answer, not a degraded one. **Handled now** (Step 1): an identity with no engine code retrieves everything for its model, and the UI says so plainly rather than calling it a "fallback".
2. **Enriching an exact match with model-wide facts.** A 1.9 TDI's own KB won't mention the sliding-door or driveshaft faults shared across the whole T5 range unless that research angle happened to surface them. Adding model-level facts *on top of* an exact match would be a genuine improvement — but only once entries are scope-aware, otherwise it reintroduces exactly the contamination above.

So the direction is: **replace tier-as-fallback with scope-as-filter.**

**Sketch of the remaining work** (not scheduled; do it when the KB is big enough that cross-variant enrichment matters):

- Add `scope: engine | model | unknown` to `ExtractedEntry` and the persisted payload. The extraction prompt already draws this distinction implicitly, so classification should be reliable; backfill existing entries with one cheap flash-lite pass (no grounding).
- Retrieval returns the union of *this identity's* entries **plus** `model`-scoped entries from sibling identities of the same brand+model — never `engine`-scoped entries from a different engine. This subsumes both the fallback case and the enrichment case in one rule.
- Per-entry provenance in the UI: show which identity each fact came from, and mute/mark inherited ones. Today the evidence table gives no hint, so a contaminated read looks identical to a clean one.
- Then reconsider whether a tier label is still meaningful at all, or whether "N facts for this exact engine + M shared across the model" is the honest presentation.

**Deliberately not doing**: splitting `knowledge_entries` into separate engine/model tables. Scope is one nullable field on an existing row; a second table would double the write paths and the retrieval joins for no gain.

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
│   ├── main.py                # FastAPI app assembly only (mounts routers)
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
│   │   └── logging.py         # record_llm_call: persists LLMCallResult into llm_calls
│   ├── vehicles/
│   │   └── identity.py        # canonical vehicle identity extraction (LLM-assisted)
│   ├── knowledge/
│   │   ├── sources/           # KnowledgeSource protocol + web_search.py (Gemini grounding, the active source)
│   │   ├── extraction.py      # grounded research text → typed KnowledgeEntry rows
│   │   ├── builder.py         # capped, progressive knowledge-collection job
│   │   └── retrieval.py       # tiered KB summary for a given vehicle identity
│   ├── criteria/
│   │   ├── profiles/          # buyer-criteria sets as YAML data (camper.yaml) — the source of truth
│   │   └── loader.py          # generic validate + upsert-by-slug into buyer_criteria_profiles
│   ├── analysis/
│   │   ├── comparables.py     # similarity retrieval: nearest-N by identity/mileage/year/power, with relaxed-match fallback
│   │   ├── pricing.py         # formats retrieved comparables (with deltas) for the judgment prompt
│   │   ├── condition.py       # LLM condition/red-flag analysis (this ad only)
│   │   ├── criteria.py        # LLM buyer-criteria fit (this ad vs. a profile's aspects); prompt built from the profile row
│   │   ├── reliability_score.py # deterministic model-reliability read from structured KB fields
│   │   ├── judgment.py        # the holistic LLM verdict call (price/condition/reliability/positives [+criteria])
│   │   ├── verdict.py         # pure build_verdict: judgment + evidence-presence → persisted shape
│   │   └── pipeline.py        # run_full_analysis: DB/LLM orchestration → one Analysis row
│   ├── services/              # HTTP/template-agnostic read functions (listings.py, knowledge.py, criteria.py)
│   ├── jobs.py                # in-process background job runner (FastAPI BackgroundTasks)
│   └── web/
│       ├── routes/            # one APIRouter per concern (dashboard, listings, runs, knowledge)
│       ├── templating.py      # shared Jinja2 templates instance
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
   - **Price**: vehicle condition is too heterogeneous for pure statistics to mean much (a low-mileage van with rust and no service history isn't comparable to a high-mileage one that's meticulously maintained), so pricing is **retrieval + LLM qualitative judgment**, not a percentile calculation. Retrieve the top N (~5–8) most similar listings from the DB by vehicle identity + mileage/year/power distance (relaxing identity match — e.g. same model/generation but different engine — if too few close matches exist, flagged as such), plus any forum-sourced price points for the same identity. Each comparable is annotated with human-readable deltas ("+15,000 km, 1 year older, …"). These annotated comparables (`pricing.py::format_comparables`) are handed to the **holistic judgment call** (see Scoring) rather than a standalone price-only LLM call — the model weighs price fairness against the ad's own condition and the comparable set together. If there are no comparables the price axis is stamped `no_data`.
   - **Reliability**: retrieve KB entries via **tiered fallback matching** — exact identity (same engine/trim) → same brand+model+generation → same brand+model — stopping at the first tier with hits, and recording which tier matched so confidence stays honest. This exists because identity extraction can fragment near-duplicate configs (observed live: "2.0 TDI 179 PS (CFCA)" vs "2.0 TDI 180 PS (CFCA biturbo)" from two ads rounding horsepower differently) — exact-only matching would make one ad's KB invisible to the other. If no tier has coverage → verdict explicitly says "no knowledge yet" and offers a one-click knowledge-collection run for that model.
   - **Buyer criteria** (only when a profile was selected for the run): a separate structured call rates the ad against each aspect of the chosen profile (`meets/partial/fails/unknown`), with the prompt assembled from the profile row. Kept out of the condition call so ad red flags and requirements-fit don't double-count. `unknown` means the ad is silent — an open question for the seller, not a failing; if *every* aspect is unknown the axis is stamped `no_data`.
   - **Verdict**: see the Scoring section below.
4. **Present**: dashboard lists analyzed listings sortable by score/price/mileage; detail page shows full breakdown with sources cited (which forum threads informed the reliability verdict).

### Scoring — holistic LLM judgment (`app/analysis/judgment.py` + `verdict.py`)

Reworked twice. The MVP's per-axis penalty formula was replaced (user decision) by **one
holistic LLM call** that weighs everything together and returns the score itself, because
hand-tuned constants read as "random formulas" and buried the qualitative reasoning the
user actually wants. It keeps **"how good a buy" (score)** separate from **"how much we
know" (confidence)**:

- **`judgment.py::judge_listing`** — a single quality LLM call that receives all the
  evidence at once (the ad's condition findings/positives, the annotated comparables, the
  KB facts, and the deterministic reliability read) and returns:
  - `overall_score` 0–100 + `recommendation` (≥70 buy_candidate · 45–69 caution · <45 avoid),
  - a `good/fair/poor` rating + short note for each axis: price, condition, reliability,
    positives (positives are `good/fair/none`, never "poor"),
  - a plain-language `reasoning` paragraph a non-expert can act on.
  The prompt is told that missing comparables/KB must lower *confidence*, not the score.
  When a buyer-criteria profile is selected, the schema becomes `JudgmentWithCriteria` and
  a fifth `criteria` axis is rated in the *same* call — how well the vehicle serves that
  buyer's stated purpose. It is evidence in the one holistic judgment, never a separately
  weighted sub-score.
- **`verdict.py::build_verdict`** — pure assembly around that call. It stamps each axis
  `no_data` (overriding the LLM's rating) when the evidence genuinely doesn't exist — no
  comparables → price `no_data`, no KB coverage → reliability `no_data` — so the UI can
  tell a neutral "fair" from a real absence of evidence (`has_data` flag). Condition and
  positives always have data (they're about this ad's own text).
  The same applies to the criteria axis: `no_data` when the ad is silent on *every*
  requirement, so an unmentioned conversion never reads as a bad one.
- **Confidence stays deterministic** (`_combined_confidence`): floored by the weaker of
  price-data presence and KB match tier, so a fluent verdict over thin data still reads as
  low confidence. Not the LLM's job. Buyer-criteria coverage is deliberately excluded
  (user decision) — price/KB measure *our* evidence stores, whereas criteria coverage
  measures what one ad happened to mention.

**Supporting structured signals still computed and persisted** (not a parallel headline
score anymore):
- **Structured condition + KB extraction** (`condition.py`, typed `KnowledgeEntry` rows):
  kept for storage/listing/future price-comparison retrieval. Condition findings stay
  listing-specific (ad red flags only) so model-general reliability doesn't double-count.
- **Deterministic reliability read** (`app/analysis/reliability_score.py`): transparent,
  symmetric rules over structured KB fields (`severity`, `onset_km`, `stance`, `sentiment`,
  tier-scaled; `strength`/positive `overall_assessment` earn bonus so aging models aren't
  ratcheted to "severe"). Now **one input to the judgment prompt** and the source of the
  strengths/concerns evidence bullets on the detail page — no longer a separate scored
  signal (the old `score_llm_variant` / two-signal dueling table is gone).

- **Buyer-criteria fit** (`app/analysis/criteria.py`, only when a profile is selected):
  typed per-aspect findings persisted to `criteria_assessments`, fed into the judgment
  prompt, and rendered as a requirements-fit evidence table on the detail page.

The verdict is stored in `Analysis` (`overall_score`, `tier`, `confidence`,
`reasoning_text`, `criteria_profile_id`; `verdict_axes` holds the per-axis
rating/note/has_data; `reliability` holds the deterministic read + KB entry ids). UI:
dashboard shows colored per-axis rating chips + muted score; detail page leads with a
verdict card, the colored axis cards, and the reasoning, then the
condition/criteria/KB evidence sections. The criteria axis and column appear only for
verdicts judged under a profile, so criteria-free verdicts render exactly as before.

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
- `analyses` — id, listing_id FK, criteria_profile_id FK (nullable — which buyer criteria this verdict was judged under), condition JSON (red flags + severities), price JSON (verdict tier, fair range, comparable_listing_ids used + their deltas, forum_price_points cited, LLM reasoning text, confidence), reliability JSON (KB entries applied, coverage level), overall_score, tier, reasoning_text, llm_model, created_at (append-only: re-analysis creates a new row)
- `buyer_criteria_profiles` — id, slug (unique), name, description, free_text (buyer's own words), flags JSON, aspects JSON (`[{key, label, prompt}]`), created_at, updated_at. Authored as YAML in `app/criteria/profiles/` and upserted by slug on startup; the file is the source of truth, the row the working copy.
- `criteria_assessments` — id, listing_id FK, profile_id FK, analysis_id FK, findings JSON (per-requirement meets/partial/fails/unknown + severity, description, supporting quote), created_at
- `knowledge_entries` — id, identity_id FK (nullable model-level vs engine-level scope), entry_type, structured payload JSON, source_url, source_quote, mention_count, confidence, created_at, updated_at
- `search_runs` — id, search_url, max_listings, criteria_profile_id FK (nullable — the profile picked on the dashboard for this scan), status, counts, started_at, finished_at (also serves as job-status table for htmx polling)
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
10. ✅ Verdict combiner (`app/analysis/verdict.py`): `run_full_analysis` orchestrates identity→condition→comparables→judgment→persist as one `Analysis` row, with deterministic confidence floored by the weaker of price-data/KB-coverage. *(The original additive combiner here was replaced by the holistic LLM judgment in Milestone H; orchestration + deterministic confidence carried forward.)*

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

**Milestone G — Scoring rework (post-MVP, ✅ but ⚠️ mostly superseded by Milestone H)** — reliability into the score + score-logic reevaluation from MVP learnings. The additive-formula scoring (19, 20, 23) was later replaced by the holistic LLM judgment; the KB enrichment (20's structured fields, 21, 22) carried forward:
19. ✅ Neutral-baseline additive score replacing the price-tier-based one: `70 + price ± + condition ± − reliability_net`. Price data-thinness (`insufficient_data`, `underpriced`) made score-neutral — thin data lowers *confidence*, not the score (fixes early-DB "everything drifts to caution").
20. ✅ Two reliability signals side by side (user decision): deterministic rules over enriched KB fields (`severity`/`onset_km`/`stance`/`sentiment`, tier-scaled, uses the listing's own mileage vs. fault onset) drive the score; the LLM's own risk read is computed in parallel as `score_llm_variant` for comparison. Condition prompt narrowed to listing-specific red flags to avoid double-counting model-general reliability.
21. ✅ Symmetric positive knowledge (user decision): new `strength` + `overall_assessment` (sentiment) entry types, balanced research angles (incl. `overall_reputation`) and extraction prompt guidance, and bonus points that offset problem penalties — counters the negativity bias where every aging model ratchets to "severe" just because breakage reports accumulate.
22. ✅ Auto-collect on first encounter: search runs give never-researched identities a first knowledge pass before their first verdict (budgeted: `auto_collect_max_identities_per_run`/`auto_collect_max_queries`, fail-soft on grounding errors; progress fragment shows the stage). *(Later fix: `execute_reanalyze` does this too. When the call lived only in `execute_search_run`, a listing whose model missed that run's collection budget stayed `reliability: no_data` permanently — nothing else would ever collect for it.)*
23. ✅ Sub-score UI (user decision): dashboard shows per-factor contribution columns (Price ± / Condition ± / Reliability ± / LLM rel. ±) with the summed score de-emphasized as a muted sort key; detail page leads with the breakdown table; re-analyze banner moved to a prominent position at the top of the page. *(Superseded by H — the signed ± columns became colored rating chips.)*

**Milestone H — Holistic LLM verdict (post-MVP, all ✅)** — user decision: rely on the LLM's overall judgment over evidence rather than hand-tuned per-axis formulas, and show one quantitative + qualitative verdict:
24. ✅ Holistic judgment call (`app/analysis/judgment.py`): one quality LLM call receives condition findings, annotated comparables (`pricing.py::format_comparables`, extracted from the old standalone price call), KB facts and the deterministic reliability read, and returns the score, recommendation, and a `good/fair/poor` rating + note per axis (price/condition/reliability/positives) plus plain-language reasoning. The separate baseline-additive scorer and the standalone price LLM call are gone.
25. ✅ `no_data` distinction (user decision): the LLM only rates good/fair/poor; `verdict.py::build_verdict` overrides an axis to `no_data`/`has_data=False` when evidence is genuinely absent (no comparables → price; no KB → reliability), so the UI shows grey "No data" vs. a neutral rating. Condition/positives always have data.
26. ✅ Structured extraction retained: `condition.py` still returns typed findings/positives and the KB keeps typed entries (for storage + future price-comparison retrieval), even though the verdict is the holistic call. `ReliabilityAssessment` dropped from the condition call — reliability now lives on the judgment call + deterministic KB read.
27. ✅ Confidence stays deterministic (user decision): `_combined_confidence`, floored by the weaker of price-data presence and KB match tier.
28. ✅ UI: dashboard per-axis colored rating chips (price/condition/reliability) + muted score; detail page a verdict card, four colored axis cards, holistic reasoning, then condition/KB evidence sections. Baseline/sum breakdown table and the two-reliability-signal dueling table removed. Verified live end-to-end: a re-analyzed T5 rendered "avoid · score 30/100 · confidence: low" with Price = grey "No data" (note explains the cheap price isn't a bargain), Condition/Reliability = red "Poor", no console errors.

**Milestone I — Cleanup & refactor for maintainability + chat readiness (post-MVP, all ✅)** — leaner code and a service/router structure the planned chat interface (Phase 2) can build on; behavior-preserving:
29. ✅ Dead-code + dependency removal: deleted the empty `app/llm/prompts/` package; removed the dormant Reddit source (needed OAuth credentials that don't exist) + its test + `reddit_*` config; dropped `praw` (Reddit-only) and `selectolax` (unused — scraping is the sidecar HTTP client) from dependencies.
30. ✅ Dropped the never-populated `VehicleIdentity.power_kw` column (migration `50f9532be7ea`); the listing-level `attributes["power_kw"]` used by comparables retrieval is unaffected.
31. ✅ Split `main.py` into one `APIRouter` per concern (`app/web/routes/{dashboard,listings,runs,knowledge}.py`); `main.py` is now app-assembly only, shared templates in `app/web/templating.py`.
32. ✅ Extracted `app/services/{listings,knowledge}.py` — HTTP/template-agnostic read functions (session in, plain data out) that back the routes now and are intended to back the chat agent's tools in Phase 2.
33. ✅ Split `verdict.py` (pure `build_verdict` scoring, no DB/LLM) from `pipeline.py` (`run_full_analysis` orchestration); renamed `Analysis.score_breakdown` → `verdict_axes` (migration `e19c46bd2fd0`, in-place ALTER preserving existing verdicts) since it's no longer a breakdown of an additive score. ✓ verify: 80 tests green; all pages served 200 live against the dev DB with correct content; token spend unchanged (no new LLM calls).

**Milestone J — Buyer-criteria profiles (post-MVP roadmap item 1, all ✅)** — judge each listing against what the buyer actually wants the vehicle *for*, built as a general mechanism with camper conversion as the first criteria set:
34. ✅ Criteria as data, not code: profiles are YAML in `app/criteria/profiles/` (slug, name, buyer's `free_text`, typed `flags`, and `aspects[{key,label,prompt}]`), upserted by slug via `app/criteria/loader.py` on startup and by `python -m app.criteria.loader`. The loader validates shape and fails loudly on a malformed profile. New tables `buyer_criteria_profiles` + `criteria_assessments`, plus `criteria_profile_id` on `analyses`/`search_runs` (migration `3eee634a1c76`, batch-mode for SQLite FK adds). `tests/test_criteria_loader.py` AST-checks that no criteria wording reaches executable code in `app/`.
35. ✅ Extraction call (`app/analysis/criteria.py`): one structured call per listing rating each profile aspect `meets/partial/fails/unknown` (+severity, description, ad quote). Kept separate from `condition.py` so ad red flags and requirements-fit don't double-count. Prompt is assembled from the profile row — the module has no knowledge of any specific criteria set.
36. ✅ Fifth axis in the *same* holistic call: `judgment.py` gains `JudgmentWithCriteria` (a sibling schema, so the no-profile path stays byte-identical) and a criteria evidence block; the model rates `criteria` alongside price/condition/reliability/positives. No criteria penalty constant, no separate criteria score.
37. ✅ `unknown` = the ad was silent, never a failure: `build_verdict` stamps the axis `no_data` when every aspect is unknown, and the UI renders "Not stated" as an open question for the seller. Criteria coverage is deliberately excluded from `_combined_confidence` (user decision) — it measures what one ad mentioned, not our evidence stores.
38. ✅ Selection model (user decision): profile dropdown on the dashboard scan form and the re-analyze form — no editor UI, no global "active profile". The choice is stored on `SearchRun` and stamped onto each `Analysis`, so a verdict always renders under the criteria it was judged with; re-analyze defaults to the previous verdict's profile.
39. ✅ UI + agent: conditional dashboard chip column (absent entirely for criteria-free verdicts), detail-page axis card, "judged for: …" label and a per-requirement evidence table; chat tools `get_criteria_profiles` + `get_listing_criteria_assessment`, and `criteria_profile_id` on `AnalysisRead`. ✓ verify: 139 tests green; app boots and loads the camper profile; pre-criteria verdicts in the dev DB render unchanged; end-to-end pipeline run confirmed 4 LLM calls with the YAML wording reaching the extraction prompt and the buyer's words reaching the judgment prompt.

> **Deferred next step (user-flagged)**: analyze listing **images** for condition and criteria. Conversion quality, interior build and damp are usually visible in photos and rarely described in the text, so a text-only read returns `unknown` for exactly the aspects that matter most. `Listing.image_urls` is already scraped and persisted, so this is an analysis-side change only.

## Verification (overall)

- Offline: `uv run pytest` — parsers against HTML fixtures, pricing math, verdict combination, LLM providers mocked.
- Live smoke: paste a real kleinanzeigen.de VW T5 search URL with max 10 listings → all 10 get verdicts; at least one knowledge-collection run seeds T5 data; re-analysis shows reliability citations.
- Budget check: `llm_calls` table shows per-run token spend within expected bounds (~2 calls/listing, 3 when a buyer-criteria profile is selected, + capped KB runs).
