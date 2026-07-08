"""Orchestrates a listing's full analysis into one persisted `Analysis` row.

The verdict itself is the holistic LLM call (`judgment.py`): it weighs price, condition,
positive signals and reliability together and returns the score, recommendation and
per-axis ratings. This module wires the evidence together, keeps the structured condition
and knowledge extraction (persisted for storage/retrieval), computes the *deterministic*
confidence label, and stamps `has_price_data` / `has_reliability_data` so the UI can tell
a neutral "fair" from a genuine absence of evidence.

Confidence is deliberately not the LLM's job (user decision): it's a mechanical function
of how much evidence we actually had — price comparables present, and how well the KB
matched the listing's identity — so a fluent verdict over thin data still reads as low
confidence.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis.comparables import ComparablesResult, find_comparables
from app.analysis.condition import ConditionAnalysis, analyze_condition
from app.analysis.judgment import Judgment, judge_listing
from app.analysis.reliability_score import ReliabilityRisk, assess_reliability_risk
from app.config import get_settings
from app.db.models import Analysis, Listing
from app.knowledge.retrieval import ReliabilitySummary, get_reliability_summary
from app.llm.provider import LLMProvider
from app.vehicles.identity import get_or_create_identity

_RANK_CONFIDENCE = {0: "low", 1: "medium", 2: "high"}
_RELIABILITY_TIER_CONFIDENCE = {"exact_identity": 2, "same_generation": 2, "same_model": 1, None: 0}


class VerdictResult(BaseModel):
    overall_score: int
    tier: Literal["buy_candidate", "caution", "avoid"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    reliability: dict
    score_breakdown: dict


def _combined_confidence(
    has_price_data: bool, reliability: ReliabilitySummary
) -> Literal["low", "medium", "high"]:
    # Floored by the weaker signal: a rich comparable set with zero reliability coverage
    # (or vice-versa) still shouldn't read as an overall "high confidence" verdict.
    price_rank = 2 if has_price_data else 0
    rel_rank = _RELIABILITY_TIER_CONFIDENCE[reliability.tier]
    return _RANK_CONFIDENCE[min(price_rank, rel_rank)]


def build_verdict(
    judgment: Judgment,
    comparables: ComparablesResult,
    reliability: ReliabilitySummary,
    det_risk: ReliabilityRisk,
    entry_ids: list[int] | None = None,
) -> VerdictResult:
    """Pure assembly: turn the holistic judgment + evidence presence into the persisted
    shape. No DB/LLM. `has_price_data` / `has_reliability_data` drive the UI's "No data"
    distinction and the deterministic confidence."""
    has_price_data = bool(comparables.comparables)
    has_reliability_data = reliability.has_coverage

    def axis(rating_obj, has_data: bool) -> dict:
        return {
            "rating": rating_obj.rating if has_data else "no_data",
            "note": rating_obj.note,
            "has_data": has_data,
        }

    return VerdictResult(
        overall_score=judgment.overall_score,
        tier=judgment.recommendation,
        confidence=_combined_confidence(has_price_data, reliability),
        reasoning=judgment.reasoning,
        reliability={
            "tier": reliability.tier,
            "entry_ids": entry_ids or [e.id for e in reliability.entries],
            "deterministic": {
                "level": det_risk.level,
                "penalty": det_risk.penalty,
                "bonus": det_risk.bonus,
                "drivers": det_risk.drivers,
                "positives": det_risk.positives,
                "has_unrated_entries": det_risk.has_unrated_entries,
            },
        },
        score_breakdown={
            "overall_score": judgment.overall_score,
            "price": axis(judgment.price, has_price_data),
            # Condition is always about this ad's own text, so it always has data.
            "condition": axis(judgment.condition, True),
            "reliability": axis(judgment.reliability, has_reliability_data),
            "positives": axis(judgment.positives, True),
        },
    )


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
        score_breakdown=verdict.score_breakdown,
        overall_score=verdict.overall_score,
        tier=verdict.tier,
        reasoning_text=verdict.reasoning,
        confidence=verdict.confidence,
        llm_model=settings.llm_model_quality,
    )
    db.add(analysis)
    db.commit()
    return analysis
