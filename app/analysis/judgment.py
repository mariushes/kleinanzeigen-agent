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

When the buyer selected a criteria profile, a fifth `criteria` axis joins the same call
(via `JudgmentWithCriteria`) — how well the vehicle serves *their* stated purpose. It is
evidence in the one holistic judgment, deliberately not a separately-weighted sub-score.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis.comparables import ComparablesResult
from app.analysis.condition import ConditionAnalysis
from app.analysis.criteria import CriteriaAnalysis
from app.analysis.pricing import PRICE_TIER_GUIDANCE, format_comparables
from app.analysis.reliability_score import ReliabilityRisk
from app.config import get_settings
from app.db.models import BuyerCriteriaProfile, Listing
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

# Appended to the system prompt only when the buyer picked a criteria profile. The axis is
# described generically — what the buyer actually wants comes from the profile row, in the
# user prompt, so no criteria set is hardcoded here.
_CRITERIA_AXIS_PROMPT = """\

This buyer also has SPECIFIC REQUIREMENTS for what they want to use the vehicle for \
(stated in the user message, with a per-aspect assessment of this ad against them). Rate \
one more axis:
- criteria: how well this specific vehicle serves the buyer's stated purpose. good = fits \
their needs well, fair = usable with compromises or unknowns, poor = a bad fit for what \
they want. Judge the fit itself, not the vehicle's general merit — a sound, fairly-priced \
van that cannot do what this buyer needs is a poor `criteria` rating.

Weigh this into the overall score too: a vehicle that doesn't serve the buyer's purpose is \
a worse buy *for them* regardless of how good it is in the abstract. Aspects marked \
`unknown` mean the ad is simply silent — treat them as open questions to raise with the \
seller, never as failures, and don't lower the score for them.
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


class JudgmentWithCriteria(Judgment):
    """The judgment schema used when a buyer-criteria profile is active.

    A sibling schema rather than a dynamically-built model: `response_schema` needs a
    static class, and this keeps the no-profile path byte-identical to what it was before
    criteria existed.
    """

    criteria: AxisRating


def _format_criteria_block(
    profile: BuyerCriteriaProfile, criteria: CriteriaAnalysis
) -> str:
    """The buyer's requirements plus this ad's per-aspect assessment, as prompt text."""
    labels = {a["key"]: a["label"] for a in (profile.aspects or [])}
    lines = [
        f"- {labels.get(f.aspect, f.aspect)}: [{f.verdict}"
        + (f"/{f.severity}" if f.severity and f.verdict != "unknown" else "")
        + f"] {f.description}"
        + (f' ("{f.supporting_quote}")' if f.supporting_quote else "")
        for f in criteria.findings
    ]
    wants = f'\nIn the buyer\'s own words: "{profile.free_text}"' if profile.free_text else ""
    return (
        f"BUYER'S REQUIREMENTS — {profile.name}: {profile.description or ''}{wants}\n"
        f"Assessment of this ad against them:\n"
        + ("\n".join(lines) or "(the ad says nothing about these requirements)")
        + f"\nRequirements summary: {criteria.summary}"
    )


def _build_user_prompt(
    listing: Listing,
    condition: ConditionAnalysis,
    comparables_block: str,
    reliability: ReliabilitySummary,
    det_risk: ReliabilityRisk,
    profile: BuyerCriteriaProfile | None = None,
    criteria: CriteriaAnalysis | None = None,
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

    criteria_block = (
        f"\n\n{_format_criteria_block(profile, criteria)}"
        if profile is not None and criteria is not None
        else ""
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
        f"{criteria_block}"
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
    profile: BuyerCriteriaProfile | None = None,
    criteria: CriteriaAnalysis | None = None,
) -> Judgment:
    """One holistic call. When a buyer-criteria profile is given, the criteria evidence is
    folded into the *same* call as a fifth axis rather than scored separately — the verdict
    stays one judgment over all the evidence, not an arithmetic combination of parts."""
    comparables_block = format_comparables(comparables, forum_price_points)
    settings = get_settings()

    has_criteria = profile is not None and criteria is not None
    response_model = JudgmentWithCriteria if has_criteria else Judgment
    system = _SYSTEM_PROMPT + (_CRITERIA_AXIS_PROMPT if has_criteria else "")

    result = provider.structured_completion(
        purpose="holistic_judgment",
        system=system,
        user=_build_user_prompt(
            listing, condition, comparables_block, reliability, det_risk, profile, criteria
        ),
        response_model=response_model,
        model=settings.llm_model_quality,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    return result.parsed
