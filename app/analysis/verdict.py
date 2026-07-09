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
"""

from typing import Literal

from pydantic import BaseModel

from app.analysis.comparables import ComparablesResult
from app.analysis.judgment import Judgment
from app.analysis.reliability_score import ReliabilityRisk
from app.knowledge.retrieval import ReliabilitySummary

_RANK_CONFIDENCE = {0: "low", 1: "medium", 2: "high"}
_RELIABILITY_TIER_CONFIDENCE = {"exact_identity": 2, "same_generation": 2, "same_model": 1, None: 0}


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
        verdict_axes={
            "overall_score": judgment.overall_score,
            "price": axis(judgment.price, has_price_data),
            # Condition is always about this ad's own text, so it always has data.
            "condition": axis(judgment.condition, True),
            "reliability": axis(judgment.reliability, has_reliability_data),
            "positives": axis(judgment.positives, True),
        },
    )
