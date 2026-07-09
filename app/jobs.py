"""Runs a search → identify → analyze pipeline in the background for one SearchRun row.

FastAPI's BackgroundTasks runs sync callables in a threadpool without blocking the
event loop, which is enough for a single-user local tool — no separate task queue
needed. This function opens its own DB session since it outlives the HTTP request that
scheduled it.
"""

from datetime import datetime, timezone

from app.analysis.pipeline import run_full_analysis
from app.config import get_settings
from app.db.models import KnowledgeEntry, KnowledgeResearchRun, Listing, SearchRun, VehicleIdentity
from app.db.session import SessionLocal
from app.knowledge.builder import build_knowledge_for_identity
from app.knowledge.sources.web_search import WebSearchSource
from app.llm.gemini import GeminiProvider
from app.llm.provider import LLMProvider
from app.scraping.ingest import run_search
from app.scraping.kleinanzeigen import KleinanzeigenClient
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

    never_researched = (
        db.query(KnowledgeResearchRun).filter(KnowledgeResearchRun.identity_id == identity.id).first()
        is None
        and db.query(KnowledgeEntry).filter(KnowledgeEntry.identity_id == identity.id).first() is None
    )
    if not never_researched:
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

            run_full_analysis(db, provider, listing)
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


def execute_reanalyze(listing_id: int, provider: LLMProvider | None = None) -> None:
    """Re-run the full analysis for one listing, e.g. after new knowledge was collected.

    Appends a fresh `Analysis` row (analysis history is append-only), so the detail page's
    latest-analysis view folds in the current knowledge base and comparables.
    """
    db = SessionLocal()
    try:
        listing = db.get(Listing, listing_id)
        if listing is None:
            return
        provider = provider or GeminiProvider()
        run_full_analysis(db, provider, listing)
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
