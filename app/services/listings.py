"""Read-side data functions for listings and their verdicts.

Deliberately HTTP- and template-agnostic: a session goes in, plain data comes out. These
are the single source of truth for "what does a listing view look like", used by the web
routes today and intended to back the chat agent's listing tools later (so the model reads
real rows via the same code, not scraped HTML).
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Analysis, Listing
from app.knowledge.retrieval import ReliabilitySummary, get_reliability_summary


def latest_analysis(listing: Listing) -> Analysis | None:
    """Most recent analysis for a listing (analysis history is append-only)."""
    if not listing.analyses:
        return None
    return max(listing.analyses, key=lambda a: a.created_at)


def list_listings(db: Session) -> list[Listing]:
    """Every listing, analyzed or not."""
    return db.query(Listing).all()


def get_listing(db: Session, listing_id: int) -> Listing | None:
    """A single listing by id, or None if it doesn't exist."""
    return db.query(Listing).filter(Listing.id == listing_id).first()


@dataclass
class ListingRow:
    listing: Listing
    analysis: Analysis


_SORT_KEYS = {
    "score": lambda row: -(row.analysis.overall_score or 0),
    "price": lambda row: row.listing.price_eur if row.listing.price_eur is not None else float("inf"),
    "mileage": lambda row: row.listing.mileage_km if row.listing.mileage_km is not None else float("inf"),
}


def list_analyzed_listings(db: Session, sort: str = "score") -> list[ListingRow]:
    """Every listing that has a verdict, as (listing, latest_analysis) rows, sorted."""
    rows = [
        ListingRow(listing=listing, analysis=analysis)
        for listing in db.query(Listing).all()
        if (analysis := latest_analysis(listing)) is not None
    ]
    rows.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["score"]))
    return rows


@dataclass
class ListingDetail:
    listing: Listing
    analysis: Analysis | None
    reliability: ReliabilitySummary
    knowledge_is_newer: bool


def get_listing_detail(db: Session, listing: Listing) -> ListingDetail:
    """A listing with its latest verdict and *live* reliability knowledge.

    Reliability is retrieved fresh rather than replayed from the frozen verdict, so
    knowledge collected after the listing was analyzed still shows. If the current KB has
    entries the verdict didn't use, `knowledge_is_newer` flags that a re-analysis would
    fold them in.
    """
    analysis = latest_analysis(listing)
    reliability = get_reliability_summary(db, listing.identity)
    used_entry_ids = set(analysis.reliability.get("entry_ids", [])) if analysis else set()
    knowledge_is_newer = analysis is not None and bool(
        {e.id for e in reliability.entries} - used_entry_ids
    )
    return ListingDetail(
        listing=listing,
        analysis=analysis,
        reliability=reliability,
        knowledge_is_newer=knowledge_is_newer,
    )
