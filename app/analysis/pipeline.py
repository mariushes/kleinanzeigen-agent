"""Per-listing analysis orchestration: gather evidence, call the LLM, persist one row.

This is the DB/LLM-facing half of the analysis (the pure scoring is `verdict.py`). It
resolves the identity, retrieves the reliability KB and comparables, runs the structured
condition call and the holistic judgment call, and writes a single `Analysis`. Structured
condition + KB extraction are persisted (not just the verdict) so they can be listed and
reused for future retrieval.
"""

from sqlalchemy.orm import Session

from app.analysis.comparables import find_comparables
from app.analysis.condition import analyze_condition
from app.analysis.judgment import judge_listing
from app.analysis.reliability_score import assess_reliability_risk
from app.analysis.verdict import build_verdict
from app.config import get_settings
from app.db.models import Analysis, Listing
from app.knowledge.retrieval import get_reliability_summary
from app.llm.provider import LLMProvider
from app.vehicles.identity import get_or_create_identity


def run_full_analysis(db: Session, provider: LLMProvider, listing: Listing) -> Analysis:
    settings = get_settings()

    if listing.identity_id is None:
        get_or_create_identity(db, provider, listing)

    reliability = get_reliability_summary(db, listing.identity)
    det_risk = assess_reliability_risk(reliability.entries, reliability.tier, listing.mileage_km)

    condition = analyze_condition(db, provider, listing)
    comparables = find_comparables(db, listing)
    judgment = judge_listing(db, provider, listing, condition, comparables, reliability, det_risk)

    verdict = build_verdict(judgment, comparables, reliability, det_risk)

    analysis = Analysis(
        listing_id=listing.id,
        condition=condition.model_dump(),
        price=judgment.price.model_dump(),
        reliability=verdict.reliability,
        verdict_axes=verdict.verdict_axes,
        overall_score=verdict.overall_score,
        tier=verdict.tier,
        reasoning_text=verdict.reasoning,
        confidence=verdict.confidence,
        llm_model=settings.llm_model_quality,
    )
    db.add(analysis)
    db.commit()
    return analysis
