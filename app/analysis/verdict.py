"""Pure verdict assembly: turn the holistic LLM judgment + evidence presence into the
persisted `Analysis` shape. No DB, no LLM — the orchestration that calls the LLM lives in
`pipeline.py`; this module is just scoring so it stays trivially testable.

The verdict itself is the holistic LLM call (`judgment.py`). Here we compute the
*deterministic* confidence label and stamp each axis `no_data` when evidence is genuinely
absent, so the UI can tell a neutral "fair" from an absence of evidence.

Confidence is deliberately not the LLM's job (user decision): it's a mechanical function
of how much evidence we actually had — price comparables present, and how well the KB
matched the listing's identity — so a fluent verdict over thin data still reads as low
confidence.

Buyer-criteria coverage is deliberately NOT part of that confidence calculation (user
decision): price and KB coverage measure *our* evidence stores, whereas criteria coverage
measures what this one ad happened to mention. Folding it in would drag every ad that
doesn't discuss the buyer's requirements down to low confidence. The criteria axis carries
its own `no_data` instead.
"""

from typing import Literal

from pydantic import BaseModel

from app.analysis.comparables import ComparablesResult
from app.analysis.criteria import CriteriaAnalysis
from app.analysis.judgment import Judgment
from app.analysis.reliability_score import ReliabilityRisk
from app.db.models import BuyerCriteriaProfile
from app.knowledge.retrieval import ReliabilitySummary

_RANK_CONFIDENCE = {0: "low", 1: "medium", 2: "high"}
_RELIABILITY_TIER_CONFIDENCE = {
    "exact_identity": 2,
    "same_generation": 2,
    # Knowledge for the whole model line, because the ad never revealed which engine it
    # is. The facts are real, but they can't be engine-specific for *this* vehicle — a
    # medium-confidence read, not a high one.
    "model_wide": 1,
    "same_model": 1,
    None: 0,
}


class VerdictResult(BaseModel):
    overall_score: int
    tier: Literal["buy_candidate", "caution", "avoid"]
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    reliability: dict
    verdict_axes: dict


def _combined_confidence(
    has_price_data: bool, reliability: ReliabilitySummary
) -> Literal["low", "medium", "high"]:
    # Floored by the weaker signal: a rich comparable set with zero reliability coverage
    # (or vice-versa) still shouldn't read as an overall "high confidence" verdict.
    price_rank = 2 if has_price_data else 0
    # .get, not [...]: an unrecognized tier must degrade to low confidence rather than
    # crash the whole verdict.
    rel_rank = _RELIABILITY_TIER_CONFIDENCE.get(reliability.tier, 0)
    return _RANK_CONFIDENCE[min(price_rank, rel_rank)]


def build_verdict(
    judgment: Judgment,
    comparables: ComparablesResult,
    reliability: ReliabilitySummary,
    det_risk: ReliabilityRisk,
    entry_ids: list[int] | None = None,
    profile: BuyerCriteriaProfile | None = None,
    criteria: CriteriaAnalysis | None = None,
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

    criteria_axis = None
    if profile is not None and criteria is not None:
        rating_obj = getattr(judgment, "criteria", None)
        if rating_obj is not None:
            # An ad silent on every aspect is an absence of evidence, not a bad fit — the
            # UI must show grey "No data", the same distinction price/reliability get.
            criteria_axis = {
                **axis(rating_obj, criteria.has_data),
                "label": profile.name,
                "profile_slug": profile.slug,
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
        verdict_axes={
            "overall_score": judgment.overall_score,
            "price": axis(judgment.price, has_price_data),
            # Condition is always about this ad's own text, so it always has data.
            "condition": axis(judgment.condition, True),
            "reliability": axis(judgment.reliability, has_reliability_data),
            "positives": axis(judgment.positives, True),
            # Only present when the buyer picked a criteria profile; absent (not empty) for
            # verdicts judged without one, so old rows render exactly as before.
            **({"criteria": criteria_axis} if criteria_axis else {}),
        },
    )
