"""The holistic verdict: one LLM call that weighs price, condition, positive signals and
model reliability *together* and returns both a quantitative score and a qualitative read.

This is the product's core judgment (user decision): rather than each axis being scored by
a separate hand-tuned formula and summed, the model sees all the evidence at once — the
listing's own red flags, the annotated comparables, the collected knowledge-base facts and
the deterministic reliability read — and rates each axis plus an overall score and
recommendation. Numbers come from a judgment over real evidence, not arithmetic on
penalties.

Per-axis ratings are `good | fair | poor` (and `good | fair | none` for positives). "No
data" is *not* something the model invents — the caller stamps `has_price_data` /
`has_reliability_data` from whether comparables / KB coverage actually exist, so the UI can
distinguish a neutral "fair" from a genuine absence of evidence. The score/recommendation
are the LLM's; confidence stays deterministic (see `verdict.py`).
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis.comparables import ComparablesResult
from app.analysis.condition import ConditionAnalysis
from app.analysis.pricing import PRICE_TIER_GUIDANCE, format_comparables
from app.analysis.reliability_score import ReliabilityRisk
from app.config import get_settings
from app.db.models import Listing
from app.knowledge.retrieval import ReliabilitySummary, format_for_prompt
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = f"""\
You are advising a buyer who doesn't know cars well on a single used-vehicle listing. \
Weigh ALL the evidence together and return one holistic verdict — a 0–100 overall score, \
a recommendation, and a rating with a short note for each axis.

Axes and ratings:
- price: is the asking price fair given the comparables? good = a clear bargain, \
fair = in line, poor = overpriced. Judge qualitatively, citing the specific comparables \
that drove it. {PRICE_TIER_GUIDANCE}
- condition: this specific ad's red flags and positive signals. good = clean, \
well-documented; fair = minor/askable issues; poor = serious flags (accident, rust, \
missing history, project car).
- reliability: how dependable this model/engine/gearbox configuration is as a purchase, \
at THIS listing's mileage. Lean on the provided knowledge-base facts and the \
deterministic risk read; if a known fault typically appears at a mileage this listing \
has already passed, weight it heavily. good = dependable for its class, fair = average, \
poor = known problematic.
- positives: standout desirable aspects of this specific ad (equipment, documentation, \
low owners). Rating is good / fair / none — positives are never "poor".

Overall score (0–100): 70 is a fairly-priced, unremarkable, dependable van with no red \
flags. Move up for bargains / strong condition / strong reliability, down for overpricing \
/ red flags / known-problematic configs. Recommendation: buy_candidate (>=70), \
caution (45–69), avoid (<45). Absence of comparables or knowledge is NOT a reason to \
lower the score — say so in the note and let it show as lower confidence instead. Write \
`reasoning` as a short plain-language paragraph a non-expert can act on.
"""


class AxisRating(BaseModel):
    rating: Literal["good", "fair", "poor", "none"]
    note: str


class Judgment(BaseModel):
    overall_score: int
    recommendation: Literal["buy_candidate", "caution", "avoid"]
    price: AxisRating
    condition: AxisRating
    reliability: AxisRating
    positives: AxisRating
    reasoning: str


def _build_user_prompt(
    listing: Listing,
    condition: ConditionAnalysis,
    comparables_block: str,
    reliability: ReliabilitySummary,
    det_risk: ReliabilityRisk,
) -> str:
    findings = (
        "\n".join(
            f"- [{f.severity}] {f.category}: {f.description}"
            + (f' ("{f.supporting_quote}")' if f.supporting_quote else "")
            for f in condition.findings
        )
        or "(none reported)"
    )
    positives = ", ".join(condition.positive_signals) or "(none reported)"

    det_lines = []
    if det_risk.drivers:
        det_lines.append("Concerns: " + "; ".join(det_risk.drivers))
    if det_risk.positives:
        det_lines.append("Strengths: " + "; ".join(det_risk.positives))
    det_block = (
        f"Deterministic reliability read: level={det_risk.level}"
        + (f"\n{chr(10).join(det_lines)}" if det_lines else "")
    )

    return (
        f"Vehicle: {listing.identity.canonical_label if listing.identity else listing.title}\n"
        f"Title: {listing.title}\n"
        f"Asking price: {listing.price_eur} EUR\n"
        f"Year: {listing.year}, Mileage: {listing.mileage_km} km\n\n"
        f"Condition summary: {condition.summary}\n"
        f"Condition red flags:\n{findings}\n"
        f"Positive signals: {positives}\n\n"
        f"Comparable listings:\n{comparables_block or '(no comparable listings available yet)'}\n\n"
        f"{format_for_prompt(reliability)}\n{det_block}"
    )


def judge_listing(
    db: Session,
    provider: LLMProvider,
    listing: Listing,
    condition: ConditionAnalysis,
    comparables: ComparablesResult,
    reliability: ReliabilitySummary,
    det_risk: ReliabilityRisk,
    forum_price_points: list[dict] | None = None,
) -> Judgment:
    comparables_block = format_comparables(comparables, forum_price_points)
    settings = get_settings()
    result = provider.structured_completion(
        purpose="holistic_judgment",
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(listing, condition, comparables_block, reliability, det_risk),
        response_model=Judgment,
        model=settings.llm_model_quality,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    return result.parsed
