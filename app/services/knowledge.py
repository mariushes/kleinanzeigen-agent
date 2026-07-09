"""Read-side data functions for the knowledge base and LLM-spend accounting.

Like `services/listings.py`: session in, plain data out, no HTTP/template coupling. Backs
the knowledge-admin pages today and the chat agent's knowledge tools later.
"""

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import KnowledgeEntry, KnowledgeResearchRun, Listing, LlmCall, VehicleIdentity


@dataclass
class IdentityCoverage:
    identity: VehicleIdentity
    entry_count: int
    listing_count: int


def list_identity_coverage(db: Session) -> list[IdentityCoverage]:
    """Every vehicle identity with how much KB coverage and how many listings it has."""
    identities = db.query(VehicleIdentity).order_by(VehicleIdentity.canonical_label).all()
    coverage = []
    for identity in identities:
        entry_count = (
            db.query(func.count(KnowledgeEntry.id))
            .filter(KnowledgeEntry.identity_id == identity.id)
            .scalar()
        )
        listing_count = (
            db.query(func.count(Listing.id)).filter(Listing.identity_id == identity.id).scalar()
        )
        coverage.append(
            IdentityCoverage(identity=identity, entry_count=entry_count, listing_count=listing_count)
        )
    return coverage


@dataclass
class LlmSpend:
    input_tokens: int
    output_tokens: int
    call_count: int


def get_llm_spend(db: Session) -> LlmSpend:
    """Aggregate token usage across all logged LLM calls (surfaced in the admin UI)."""
    totals = db.query(
        func.coalesce(func.sum(LlmCall.input_tokens), 0),
        func.coalesce(func.sum(LlmCall.output_tokens), 0),
        func.count(LlmCall.id),
    ).one()
    return LlmSpend(input_tokens=totals[0], output_tokens=totals[1], call_count=totals[2])


def list_identity_entries(db: Session, identity_id: int) -> list[KnowledgeEntry]:
    """All knowledge entries for one identity, grouped by type then confidence."""
    return (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.identity_id == identity_id)
        .order_by(KnowledgeEntry.entry_type, KnowledgeEntry.confidence.desc())
        .all()
    )


def covered_research_angles(db: Session, identity_id: int) -> list[str]:
    """Which research angles have already been collected for an identity."""
    return [
        r.angle_key
        for r in db.query(KnowledgeResearchRun)
        .filter(KnowledgeResearchRun.identity_id == identity_id)
        .all()
    ]
