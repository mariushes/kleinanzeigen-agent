"""Normalizes messy classified-ad titles into a canonical `VehicleIdentity`.

Brand-agnostic by design: nothing here hardcodes "VW" or "T5" — the LLM does the
normalization using its own knowledge of vehicle brands/models/engine variants, for
whatever brand/model shows up in the listing.

Deliberately excludes precise numeric power/displacement from the identity-matching key
(`canonical_label`): rounding PS→kW during scraping can wobble by a kW between two
listings of the exact same real-world configuration, which would fragment the knowledge
base into near-duplicate identities. Numeric power/fuel are kept for comparables
distance calculations (on `Listing.attributes`), not for identity dedup.
"""

import re

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing, VehicleIdentity
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = """\
You normalize noisy German used-vehicle classified-ad data into a canonical vehicle \
identity. Classified titles are often keyword-stuffed for search (e.g. dealer spam \
mentioning multiple unrelated models) — extract the identity of the actual vehicle \
being sold, not every keyword in the title.

Rules:
- Use the official manufacturer brand name (e.g. "Volkswagen", not "VW").
- `model` is the model line as the manufacturer names it (e.g. "T5 Multivan", "Golf").
- `generation` is only the generation/facelift code if explicitly identifiable (e.g. \
"T5.1", "Mk7"). Leave it null if not clearly determinable — do not guess from the year.
- `engine_code` should describe the engine/power variant using standard terminology \
you know for this model, e.g. "2.0 TDI 180 PS (CFCA biturbo)". Leave null if you cannot \
identify it confidently from the given data.
- `trim` is the trim/spec level if named (e.g. "Highline", "Match", "Comfortline"). \
Leave null if not stated.
- `displacement_l` and `fuel` are informational best-effort extractions, not used for \
matching — null is fine if unclear.
"""


class ExtractedIdentity(BaseModel):
    brand: str
    model: str
    generation: str | None = None
    engine_code: str | None = None
    trim: str | None = None
    displacement_l: float | None = None
    fuel: str | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_canonical_label(
    brand: str,
    model: str,
    generation: str | None,
    engine_code: str | None,
    trim: str | None,
) -> str:
    parts = [_normalize(brand), _normalize(model)]
    if generation:
        parts.append(_normalize(generation))
    if trim:
        parts.append(_normalize(trim))
    if engine_code:
        parts.append(_normalize(engine_code))
    return " | ".join(parts)


def _build_user_prompt(listing: Listing) -> str:
    attrs = listing.attributes or {}
    raw_details = attrs.get("raw_details", {})
    return (
        f"Title: {listing.title}\n"
        f"Raw details: {raw_details}\n"
        f"Vehicle type: {attrs.get('vehicle_type')}\n"
        f"Description (first 400 chars): {(listing.description_text or '')[:400]}"
    )


def get_or_create_identity(
    db: Session, provider: LLMProvider, listing: Listing
) -> VehicleIdentity:
    settings = get_settings()
    result = provider.structured_completion(
        purpose="vehicle_identity",
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(listing),
        response_model=ExtractedIdentity,
        model=settings.llm_model_fast,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    extracted: ExtractedIdentity = result.parsed

    canonical_label = build_canonical_label(
        extracted.brand, extracted.model, extracted.generation, extracted.engine_code, extracted.trim
    )

    # Case-insensitive: the LLM isn't perfectly consistent about casing across calls
    # (e.g. "Volkswagen" vs "volkswagen"), so match loosely to avoid duplicate identities.
    identity = (
        db.query(VehicleIdentity)
        .filter(func.lower(VehicleIdentity.canonical_label) == canonical_label.lower())
        .first()
    )
    if identity is None:
        identity = VehicleIdentity(
            brand=_normalize(extracted.brand),
            model=_normalize(extracted.model),
            generation=extracted.generation,
            engine_code=extracted.engine_code,
            displacement_l=extracted.displacement_l,
            fuel=extracted.fuel,
            trim=extracted.trim,
            canonical_label=canonical_label,
        )
        db.add(identity)
        db.flush()

    listing.identity_id = identity.id
    db.commit()
    return identity


def identify_listings(db: Session, provider: LLMProvider, listings: list[Listing]) -> dict[int, VehicleIdentity]:
    """Identify each listing, skipping ones that already have a cached identity."""
    result: dict[int, VehicleIdentity] = {}
    for listing in listings:
        if listing.identity_id is not None:
            result[listing.id] = listing.identity
            continue
        result[listing.id] = get_or_create_identity(db, provider, listing)
    return result
