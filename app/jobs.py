"""Runs a search → identify → analyze pipeline in the background for one SearchRun row.

FastAPI's BackgroundTasks runs sync callables in a threadpool without blocking the
event loop, which is enough for a single-user local tool — no separate task queue
needed. This function opens its own DB session since it outlives the HTTP request that
scheduled it.
"""

from datetime import datetime, timezone

from app.analysis.verdict import run_full_analysis
from app.config import get_settings
from app.db.models import Listing, SearchRun, VehicleIdentity
from app.db.session import SessionLocal
from app.knowledge.builder import build_knowledge_for_identity
from app.knowledge.sources.web_search import WebSearchSource
from app.llm.gemini import GeminiProvider
from app.llm.provider import LLMProvider
from app.scraping.ingest import run_search
from app.scraping.kleinanzeigen import KleinanzeigenClient


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
        }
        db.commit()

        listings = db.query(Listing).filter(Listing.id.in_(ingest_result.listing_ids)).all()
        for i, listing in enumerate(listings, start=1):
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
