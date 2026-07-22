"""Runs a search → identify → analyze pipeline in the background for one SearchRun row.

FastAPI's BackgroundTasks runs sync callables in a threadpool without blocking the
event loop, which is enough for a single-user local tool — no separate task queue
needed. This function opens its own DB session since it outlives the HTTP request that
scheduled it.
"""

from datetime import datetime, timezone

from app.analysis.pipeline import run_full_analysis
from app.config import get_settings
from app.db.models import KnowledgeEntry, Listing, SearchRun, VehicleIdentity
from app.db.session import SessionLocal
from app.knowledge.builder import build_knowledge_for_identity
from app.knowledge.sources.web_search import WebSearchSource
from app.llm.gemini import GeminiProvider
from app.llm.provider import LLMProvider
from app.scraping.ingest import run_search
from app.scraping.kleinanzeigen import KleinanzeigenClient
from app.services.criteria import get_profile
from app.vehicles.identity import get_or_create_identity


def _maybe_auto_collect(
    db, provider: LLMProvider, identity: VehicleIdentity, collected_identity_ids: set[int]
) -> bool:
    """First knowledge pass for a never-researched identity, so the first verdict
    already has reliability data. Budgeted per search run; fail-soft: a grounding
    failure (quota, network) must never break the listing analysis itself."""
    settings = get_settings()
    if not settings.auto_collect_enabled:
        return False
    if identity.id in collected_identity_ids:
        return False
    if len(collected_identity_ids) >= settings.auto_collect_max_identities_per_run:
        return False

    # Gate on *entries*, not on whether a research run was logged. A run that was
    # interrupted (or that found nothing) leaves an angle recorded with zero entries, and
    # keying off the run alone marked such an identity "researched" forever — it could
    # never acquire knowledge again. Retrying is cheap and bounded: the builder consumes
    # only angles not yet covered for this identity, so it advances rather than repeats.
    has_knowledge = (
        db.query(KnowledgeEntry).filter(KnowledgeEntry.identity_id == identity.id).first()
        is not None
    )
    if has_knowledge:
        return False

    collected_identity_ids.add(identity.id)
    try:
        build_knowledge_for_identity(
            db,
            provider,
            WebSearchSource(provider, db),
            identity,
            max_queries=settings.auto_collect_max_queries,
        )
    except Exception:  # noqa: BLE001
        db.rollback()
    return True


def execute_search_run(
    search_run_id: int,
    client: KleinanzeigenClient | None = None,
    provider: LLMProvider | None = None,
) -> None:
    db = SessionLocal()
    try:
        search_run = db.get(SearchRun, search_run_id)
        search_run.status = "running"
        search_run.started_at = datetime.now(timezone.utc)
        db.commit()

        provider = provider or GeminiProvider()
        ingest_result = run_search(db, search_run.search_url, search_run.max_listings, client=client)

        search_run.counts = {
            "target": search_run.max_listings,
            "scraped": ingest_result.total_stored,
            "skipped_wanted_ads": ingest_result.skipped_wanted_ads,
            "analyzed": 0,
            "knowledge_collected": 0,
        }
        db.commit()

        listings = db.query(Listing).filter(Listing.id.in_(ingest_result.listing_ids)).all()
        # The criteria profile chosen on the dashboard when this run was started. Read once
        # from the run so every listing in the run is judged under the same criteria, even
        # if the selection changes while the run is in flight.
        profile = get_profile(db, search_run.criteria_profile_id)
        collected_identity_ids: set[int] = set()
        for i, listing in enumerate(listings, start=1):
            # Identity first (normally done inside run_full_analysis) so a brand-new
            # model gets its first knowledge pass before its first verdict.
            if listing.identity_id is None:
                get_or_create_identity(db, provider, listing)
            if _maybe_auto_collect(db, provider, listing.identity, collected_identity_ids):
                search_run.counts = {
                    **search_run.counts,
                    "knowledge_collected": len(collected_identity_ids),
                }
                db.commit()

            run_full_analysis(db, provider, listing, profile=profile)
            search_run.counts = {**search_run.counts, "analyzed": i}
            db.commit()

        search_run.status = "done"
        search_run.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - reported via SearchRun.error, not re-raised
        search_run.status = "error"
        search_run.error = str(exc)
        search_run.finished_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def execute_reanalyze(
    listing_id: int,
    provider: LLMProvider | None = None,
    criteria_profile_id: int | None = None,
) -> None:
    """Re-run the full analysis for one listing, e.g. after new knowledge was collected.

    Appends a fresh `Analysis` row (analysis history is append-only), so the detail page's
    latest-analysis view folds in the current knowledge base and comparables. If the
    listing's model has never been researched, a first knowledge pass is collected first
    (budgeted, fail-soft) — otherwise a listing that missed the search run's collection
    budget could never acquire reliability data.

    `criteria_profile_id` comes from the re-analyze form, which defaults to whatever the
    previous verdict was judged under — so a plain "re-analyze" keeps the same criteria,
    and switching criteria is an explicit choice.
    """
    db = SessionLocal()
    try:
        listing = db.get(Listing, listing_id)
        if listing is None:
            return
        provider = provider or GeminiProvider()

        # Same first-pass collection the search run does: a listing whose model has never
        # been researched would otherwise be re-analyzed knowledge-blind forever, since
        # nothing else triggers collection for it. `_maybe_auto_collect` is idempotent
        # (it no-ops once entries or research runs exist) and fail-soft.
        if listing.identity_id is None:
            get_or_create_identity(db, provider, listing)
        _maybe_auto_collect(db, provider, listing.identity, set())

        run_full_analysis(db, provider, listing, profile=get_profile(db, criteria_profile_id))
    finally:
        db.close()


def execute_knowledge_run(identity_id: int, provider: LLMProvider | None = None) -> None:
    """Collect reliability knowledge for one vehicle identity, on demand from the admin UI.

    Kept separate from the search-run pipeline: knowledge collection is deliberately
    manual (it spends grounded free-tier quota) rather than firing automatically for
    every identity a scrape happens to surface.
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        identity = db.get(VehicleIdentity, identity_id)
        if identity is None:
            return
        provider = provider or GeminiProvider()
        source = WebSearchSource(provider, db)
        build_knowledge_for_identity(
            db, provider, source, identity, max_queries=settings.knowledge_default_max_queries
        )
    finally:
        db.close()
