"""LLM-based condition/red-flag analysis of a listing's description and attributes.

This only produces a `ConditionAnalysis` — persisting it into an `Analysis` row happens
in `verdict.py`, which combines condition + price + reliability into one row per run.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = """\
You are assessing the condition of a used vehicle from its classified-ad description \
and attributes, for a buyer who doesn't know cars well. Read critically — sellers \
often phrase problems euphemistically or omit them entirely.

Look specifically for these red-flag categories (use these category names when they \
apply; use another short snake_case category name for anything else relevant):
- accident_history: mentions of past accidents/damage ("Unfallschaden", "Vorschaden")
- rust: rust or corrosion mentioned ("Rost", "Durchrostung")
- missing_service_history: no mention of service records where you'd expect one, or \
explicitly stated as missing ("kein Scheckheft")
- vague_wording: description is evasive or unusually vague about mechanical condition
- tuv_expired: inspection (TÜV/HU) expired or expiring very soon
- project_car: sold explicitly as a project/parts/non-running vehicle ("Bastlerfahrzeug", \
"Bastlerauto", "nicht fahrbereit", "Export")
- suspiciously_cheap: price is far below what this configuration/mileage would suggest, \
which often signals hidden problems
- oil_or_fluid_issues: mentions of oil consumption, leaks, or fluid problems
- mileage_inconsistency: mileage seems inconsistent with the vehicle's age/history

Also note genuine positive signals (full service history / "Scheckheftgepflegt", recent \
TÜV, single owner, recent major maintenance, etc.) in `positive_signals`.

Severity: "high" = would make you walk away or renegotiate hard; "medium" = worth \
asking the seller about; "low" = minor, cosmetic, or just worth noting.

Only report findings actually supported by the given text/attributes — do not invent \
problems that aren't there. If the listing reads clean, return an empty findings list \
and say so in the summary.
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
        f"Title: {listing.title}\n"
        f"Price: {listing.price_eur} EUR\n"
        f"Year: {listing.year}, Mileage: {listing.mileage_km} km\n"
        f"Condition label: {attrs.get('condition_label')}\n"
        f"Features: {attrs.get('features')}\n"
        f"Raw details: {attrs.get('raw_details')}\n"
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
