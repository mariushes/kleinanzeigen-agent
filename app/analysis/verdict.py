"""Combines condition + price + reliability into one persisted `Analysis` row.

Deliberately deterministic, not a fourth LLM call: condition and price analysis already
each cost one quality-model call per listing (two, not the one originally sketched in
PLAN.md's token-budget note — price analysis needs condition's output as input, so they
can't be merged into a single round trip). Given the free-tier quota constraints
discovered during build (see CLAUDE.md), a rule-based combiner keeps per-listing cost at
2 quality calls + 1 fast call, stays fully testable without mocking an LLM, and is easy
to tune as plain constants below.

Reliability knowledge doesn't yet feed the numeric score: `KnowledgeEntry.payload` won't
have a defined, scoreable shape until Milestone E's extraction schema exists. For now it
only informs `confidence` and appears as citations in the reasoning text.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis.comparables import find_comparables
from app.analysis.condition import ConditionAnalysis, analyze_condition
from app.analysis.pricing import PriceVerdict, analyze_price
from app.config import get_settings
from app.db.models import Analysis, Listing
from app.knowledge.retrieval import ReliabilitySummary, format_for_prompt, get_reliability_summary
from app.llm.provider import LLMProvider
from app.vehicles.identity import get_or_create_identity

_SEVERITY_PENALTY = {"high": 25, "medium": 10, "low": 3}
_POSITIVE_SIGNAL_BONUS = 2
_MAX_POSITIVE_BONUS = 10
_PRICE_TIER_BASE_SCORE = {"underpriced": 75, "fair": 70, "overpriced": 45, "insufficient_data": 60}

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_RELIABILITY_TIER_CONFIDENCE = {"exact_identity": 2, "same_generation": 2, "same_model": 1, None: 0}


class Verdict(BaseModel):
    overall_score: int
    tier: Literal["buy_candidate", "caution", "avoid"]
    reasoning: str
    confidence: Literal["low", "medium", "high"]


def _score_tier(score: int) -> Literal["buy_candidate", "caution", "avoid"]:
    if score >= 70:
        return "buy_candidate"
    if score >= 45:
        return "caution"
    return "avoid"


def _combined_confidence(price: PriceVerdict, reliability: ReliabilitySummary) -> Literal["low", "medium", "high"]:
    price_rank = _CONFIDENCE_RANK[price.confidence]
    reliability_rank = _RELIABILITY_TIER_CONFIDENCE[reliability.tier]
    # Floored by the weaker of the two signals: a confident price verdict with zero
    # reliability coverage still shouldn't read as an overall "high confidence" verdict.
    overall_rank = min(price_rank, reliability_rank)
    return {0: "low", 1: "medium", 2: "high"}[overall_rank]


def _build_reasoning(condition: ConditionAnalysis, price: PriceVerdict, reliability: ReliabilitySummary) -> str:
    parts = [f"Condition: {condition.summary}", f"Price: {price.reasoning}"]
    if reliability.has_coverage:
        parts.append(format_for_prompt(reliability))
    else:
        parts.append(
            "Reliability: no knowledge base coverage yet for this configuration — "
            "consider running a knowledge-collection pass for this model."
        )
    return "\n\n".join(parts)


def combine_verdict(
    condition: ConditionAnalysis, price: PriceVerdict, reliability: ReliabilitySummary
) -> Verdict:
    score = _PRICE_TIER_BASE_SCORE[price.tier]
    score -= sum(_SEVERITY_PENALTY[f.severity] for f in condition.findings)
    score += min(len(condition.positive_signals) * _POSITIVE_SIGNAL_BONUS, _MAX_POSITIVE_BONUS)
    score = max(0, min(100, score))

    return Verdict(
        overall_score=score,
        tier=_score_tier(score),
        reasoning=_build_reasoning(condition, price, reliability),
        confidence=_combined_confidence(price, reliability),
    )


def run_full_analysis(db: Session, provider: LLMProvider, listing: Listing) -> Analysis:
    settings = get_settings()

    if listing.identity_id is None:
        get_or_create_identity(db, provider, listing)

    condition = analyze_condition(db, provider, listing)
    comparables = find_comparables(db, listing)
    price = analyze_price(db, provider, listing, condition.summary, comparables)
    reliability = get_reliability_summary(db, listing.identity)
    verdict = combine_verdict(condition, price, reliability)

    analysis = Analysis(
        listing_id=listing.id,
        condition=condition.model_dump(),
        price=price.model_dump(),
        reliability={
            "tier": reliability.tier,
            "entry_ids": [e.id for e in reliability.entries],
        },
        overall_score=verdict.overall_score,
        tier=verdict.tier,
        reasoning_text=verdict.reasoning,
        llm_model=settings.llm_model_quality,
    )
    db.add(analysis)
    db.commit()
    return analysis
