"""Normalizes messy classified-ad titles into a canonical `VehicleIdentity`.

Brand-agnostic by design: nothing here hardcodes "VW" or "T5" — the LLM does the
normalization using its own knowledge of vehicle brands/models/engine variants, for
whatever brand/model shows up in the listing.

The `canonical_label` is a MATCHING KEY, not a display string: two ads for the same
real-world vehicle must produce the same label, or their shared reliability knowledge is
split across two identities. Everything unstable is therefore kept out of it:

- **Numeric power/displacement**: rounding PS→kW during scraping wobbles by a kW between
  identical vehicles. Kept on `Listing.attributes` for comparables distance, not for dedup.
- **The power figure inside the engine string**: the same engine is advertised as
  "1.9 TDI 102 PS" and "1.9 TDI 105 PS", so the key uses `engine_family` ("1.9 TDI"),
  which the LLM derives. Deliberately *not* a regex — a pattern list like `tdi|tsi|cdi`
  is one brand's naming convention masquerading as a general rule, and would silently
  mangle BMW/Ford/Tesla engines. The full `engine_code` is kept on the row for display.
- **Trim**: varies with how much the seller wrote, and describes equipment rather than
  the mechanical configuration reliability knowledge is about.

The `model`/`generation` boundary is pinned in the prompt with worked examples, because
the LLM previously split the same vehicle two ways — model "Transporter" + generation
"T5" vs. model "T5 Transporter" — which fragmented the KB and defeated even the
`same_model` retrieval tier.

*Idea if `engine_family` proves inconsistent across calls (not implemented):* pass the
distinct `engine_family` values already stored for that brand as few-shot examples in the
prompt, so the model reuses an existing key instead of inventing a new spelling.
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

These fields are a MATCHING KEY: two ads for the same real-world vehicle must produce \
the same values, or their shared knowledge gets split in two. Put each piece of \
information in the same field every time.

Rules:
- Use the official manufacturer brand name (e.g. "Volkswagen", not "VW").
- `model` is the full model line as the manufacturer names it, INCLUDING the series \
designation when that is part of the name: "T5 Transporter", "T6 Multivan", "Golf", \
"Sprinter". Do NOT strip the series out into `generation` — "Transporter" with \
generation "T5" is WRONG; it must be model "T5 Transporter". If an ad names only the \
series ("T5") and the body style is clear from the data, still produce the combined \
form ("T5 Transporter", "T5 Multivan").
- `generation` is ONLY a facelift/sub-generation marker *within* that model line, e.g. \
"T5.1", "Mk7.5". It is almost always null. Never put the series designation here if it \
already belongs in `model`, and never guess it from the year.
- `engine_code` describes the engine variant using standard terminology for this model, \
e.g. "2.0 TDI 180 PS (CFCA biturbo)". Leave null if you cannot identify it confidently \
from the given data — a wrong guess is worse than null, because null correctly means \
"this ad didn't say" and is handled as such.
- `engine_family` is the SAME engine without the power figure or internal code — the \
designation the manufacturer uses to name the engine variant itself, in whatever \
convention applies to that brand: "1.9 TDI", "2.0 TDI", "320d", "1.5 EcoBoost", \
"Long Range". This is the matching key, so be consistent: the same real engine \
advertised as "1.9 TDI 102 PS" in one ad and "1.9 TDI 105 PS" in another must give \
`engine_family` "1.9 TDI" both times. Null whenever `engine_code` is null.
- `trim` is the trim/spec level if named (e.g. "Highline", "Match", "Comfortline"). \
Leave null if not stated.
- `displacement_l` and `fuel` are informational best-effort extractions, not used for \
matching — null is fine if unclear.

Worked examples:
- "VW T5 Transporter Kombi 1.9 TDI 102 PS" -> brand "Volkswagen", model \
"T5 Transporter", generation null, engine_code "1.9 TDI 102 PS", engine_family "1.9 TDI".
- "VW T5 Transporter lang, top gepflegt" (no engine stated) -> brand "Volkswagen", \
model "T5 Transporter", generation null, engine_code null, engine_family null.
"""


class ExtractedIdentity(BaseModel):
    brand: str
    model: str
    generation: str | None = None
    engine_code: str | None = None
    # Coarse engine designation without the power figure — the part of the engine that
    # goes into the matching key. See the prompt's rules and `build_canonical_label`.
    engine_family: str | None = None
    trim: str | None = None
    displacement_l: float | None = None
    fuel: str | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_canonical_label(
    brand: str,
    model: str,
    generation: str | None,
    engine_family: str | None,
    trim: str | None = None,
) -> str:
    """The identity/matching key: only the stable parts of the extraction.

    `engine_family` (not `engine_code`) is used, so the same engine advertised with
    slightly different power figures collapses to one identity. `trim` is accepted for
    call compatibility but deliberately ignored — it varies with how much the seller
    chose to write and describes equipment, not the mechanical configuration that
    reliability knowledge is about.
    """
    parts = [_normalize(brand), _normalize(model)]
    if generation:
        parts.append(_normalize(generation))
    if engine_family:
        parts.append(_normalize(engine_family))
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
        extracted.brand, extracted.model, extracted.generation, extracted.engine_family
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
