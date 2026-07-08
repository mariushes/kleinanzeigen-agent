"""LLM red-flag analysis of a single listing's text.

Produces structured findings about THIS ad only — accident, rust, service history,
evasive wording, TÜV, project-car, suspiciously-cheap — plus positive signals. It is
deliberately *not* about the model's general reputation: that model/engine reliability
judgment lives on the reliability axis (KB rules in `reliability_score.py`) and in the
holistic `judgment.py` call, so it isn't double-counted here.

The structured output is persisted (in `verdict.py`) so red flags can be stored, listed,
and reused later; its summary is also fed into the holistic judgment call as evidence.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = """\
You assess a used-vehicle classified ad for a buyer who doesn't know cars well. Report \
CONDITION RED FLAGS — problems evident from THIS specific ad's text and stated facts \
only. Read critically; sellers phrase problems euphemistically. Use these categories \
when they apply (else a short snake_case name):
- accident_history ("Unfallschaden", "Vorschaden")
- rust ("Rost", "Durchrostung")
- missing_service_history ("kein Scheckheft", or none mentioned where expected)
- vague_wording (evasive about mechanical condition)
- tuv_expired (TÜV/HU expired or expiring very soon)
- project_car ("Bastlerfahrzeug", "nicht fahrbereit", "Export")
- suspiciously_cheap (price far below what the config/mileage implies — often hides problems)
- damage_disclosed (any other specific damage/fault the ad itself states)

Do NOT put the model's general reputation or "this engine is known to fail" reasoning \
here — that is judged separately on the reliability axis. A genuinely high odometer \
reading stated in the ad is a fact you may note; but *why* high mileage is risky for \
this engine is not a condition finding. Note real positive signals (Scheckheftgepflegt, \
recent TÜV, single owner, recent major maintenance) in `positive_signals`. Severity: \
high = walk away / renegotiate hard; medium = ask the seller; low = minor/cosmetic. \
Only report findings supported by the given text. Write a one-line `summary`.
"""


class ConditionFinding(BaseModel):
    category: str
    severity: Literal["low", "medium", "high"]
    description: str
    supporting_quote: str | None = None


class ConditionAnalysis(BaseModel):
    findings: list[ConditionFinding]
    positive_signals: list[str]
    summary: str


def _build_user_prompt(listing: Listing) -> str:
    attrs = listing.attributes or {}
    return (
        f"Vehicle: {listing.identity.canonical_label if listing.identity else listing.title}\n"
        f"Title: {listing.title}\n"
        f"Price: {listing.price_eur} EUR\n"
        f"Year: {listing.year}, Mileage: {listing.mileage_km} km\n"
        f"Condition label: {attrs.get('condition_label')}\n"
        f"Features: {attrs.get('features')}\n"
        f"Raw details: {attrs.get('raw_details')}\n\n"
        f"Full description:\n{listing.description_text or ''}"
    )


def analyze_condition(db: Session, provider: LLMProvider, listing: Listing) -> ConditionAnalysis:
    settings = get_settings()
    result = provider.structured_completion(
        purpose="condition_analysis",
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(listing),
        response_model=ConditionAnalysis,
        model=settings.llm_model_quality,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    return result.parsed
