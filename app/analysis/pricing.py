"""Qualitative LLM price verdict, built from retrieved comparables (see `comparables.py`).

Deliberately not a statistical percentile/median: vehicle condition varies too much for
that to mean anything (a rough high-mileage van isn't comparable to a pristine one at
the same mileage). Instead we hand the LLM the target listing, its condition-analysis
summary, and the closest comparables annotated with human-readable deltas, and ask it
to reason about fairness given those *specific* differences — the same way a knowledgeable
friend would eyeball a handful of similar ads, not run statistics on them.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis.comparables import ComparablesResult
from app.config import get_settings
from app.db.models import Listing
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = """\
You are judging whether the asking price of a used vehicle listing is fair, given a \
handful of comparable listings for the same or a closely related configuration.

Reason qualitatively, the way a knowledgeable friend would: look at each comparable's \
price together with how it differs from the target (mileage, age, power, tier of \
match) and the target's own condition assessment, then judge whether the asking price \
makes sense relative to them. Do not just average the comparable prices — a comparable \
that is much rougher condition or a worse-match tier should count for less.

Comparables are grouped by match tier:
- "exact_identity": same brand/model/generation/engine variant — most reliable anchor
- "same_generation": same brand/model/generation, different engine/trim — decent anchor
- "same_model": same brand/model only, possibly different generation/engine — weakest \
anchor, treat with caution

If there are zero or only very weak/few comparables, or they disagree wildly, say so \
and use tier="insufficient_data" with confidence="low" rather than guessing a number \
that looks precise but isn't grounded in anything.

Cite the specific comparables that drove your judgment in the reasoning text (e.g. by \
their title or price), not just "based on the comparables".
"""


class FairPriceRange(BaseModel):
    low_eur: int
    high_eur: int


class PriceVerdict(BaseModel):
    tier: Literal["underpriced", "fair", "overpriced", "insufficient_data"]
    fair_price_range: FairPriceRange | None
    reasoning: str
    confidence: Literal["low", "medium", "high"]


def _insufficient_data_verdict(reason: str) -> PriceVerdict:
    return PriceVerdict(
        tier="insufficient_data",
        fair_price_range=None,
        reasoning=reason,
        confidence="low",
    )


def _build_user_prompt(
    listing: Listing,
    condition_summary: str,
    comparables: ComparablesResult,
    forum_price_points: list[dict] | None,
) -> str:
    lines = [
        f"Target listing: {listing.title}",
        f"Asking price: {listing.price_eur} EUR",
        f"Year: {listing.year}, Mileage: {listing.mileage_km} km",
        f"Condition summary: {condition_summary}",
        "",
        "Comparables:",
    ]
    for comparable in comparables.comparables:
        c = comparable.listing
        lines.append(
            f"- [{comparable.tier}] \"{c.title}\" — {c.price_eur} EUR ({comparable.delta_description})"
        )

    if forum_price_points:
        lines.append("")
        lines.append("Forum-mentioned price points (secondary, less verified signal):")
        for point in forum_price_points:
            lines.append(
                f"- {point.get('price_eur')} EUR, {point.get('context', '')} (source: {point.get('source_url')})"
            )

    return "\n".join(lines)


def analyze_price(
    db: Session,
    provider: LLMProvider,
    listing: Listing,
    condition_summary: str,
    comparables: ComparablesResult,
    forum_price_points: list[dict] | None = None,
) -> PriceVerdict:
    if not comparables.comparables and not forum_price_points:
        return _insufficient_data_verdict(
            "No comparable listings or forum price points are available yet for this vehicle configuration."
        )

    settings = get_settings()
    result = provider.structured_completion(
        purpose="price_analysis",
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(listing, condition_summary, comparables, forum_price_points),
        response_model=PriceVerdict,
        model=settings.llm_model_quality,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    return result.parsed
