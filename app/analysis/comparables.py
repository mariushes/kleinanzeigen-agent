"""Nearest-comparable retrieval for price analysis.

Vehicle condition varies too much for a pure statistical price band to mean anything
(see `pricing.py`), so what we need here is not a sample-size-driven average but the N
*closest* listings to reason about qualitatively. "Closest" is tiered by identity
specificity first (exact engine/trim match beats same-model-different-engine), then by
mileage/year/power distance within a tier — the same tiered-fallback pattern used for
knowledge-base retrieval, applied to comparables instead.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing, VehicleIdentity

# "Decent" tiers are close enough to actually anchor a price judgment; "same_model" alone
# (different generation/engine) is included to avoid an empty result, but should read as
# low-confidence to the caller.
_DECENT_TIERS = {"exact_identity", "same_generation"}


@dataclass
class Comparable:
    listing: Listing
    tier: str
    delta_description: str


@dataclass
class ComparablesResult:
    comparables: list[Comparable] = field(default_factory=list)
    tier_counts: dict[str, int] = field(default_factory=dict)

    @property
    def decent_count(self) -> int:
        return sum(count for tier, count in self.tier_counts.items() if tier in _DECENT_TIERS)


def _power_kw(listing: Listing) -> int | None:
    return (listing.attributes or {}).get("power_kw")


def _distance(target: Listing, candidate: Listing) -> float:
    mileage_delta = abs((candidate.mileage_km or 0) - (target.mileage_km or 0))
    year_delta = abs((candidate.year or 0) - (target.year or 0))
    power_delta = abs((_power_kw(candidate) or 0) - (_power_kw(target) or 0))
    # Heuristic weights: ~10,000 km ~= 1 model year ~= 10 kW of difference.
    return mileage_delta / 10_000 + year_delta * 2 + power_delta / 10


def _delta_description(target: Listing, candidate: Listing) -> str:
    parts: list[str] = []

    if target.mileage_km is not None and candidate.mileage_km is not None:
        diff = candidate.mileage_km - target.mileage_km
        sign = "+" if diff >= 0 else ""
        parts.append(f"{sign}{diff:,} km".replace(",", ","))

    if target.year is not None and candidate.year is not None:
        diff = candidate.year - target.year
        if diff == 0:
            parts.append("same year")
        else:
            unit = "year" if abs(diff) == 1 else "years"
            parts.append(f"{abs(diff)} {unit} {'newer' if diff > 0 else 'older'}")

    target_power, candidate_power = _power_kw(target), _power_kw(candidate)
    if target_power is not None and candidate_power is not None:
        parts.append("same power" if target_power == candidate_power else f"{candidate_power}kW vs {target_power}kW")

    return ", ".join(parts) if parts else "insufficient data for comparison"


def find_comparables(
    db: Session,
    listing: Listing,
    target_count: int | None = None,
) -> ComparablesResult:
    settings = get_settings()
    target_count = target_count or settings.comparables_target_count

    if listing.identity_id is None:
        return ComparablesResult()

    identity = listing.identity
    result = ComparablesResult()
    seen_ids = {listing.id}

    tiers: list[tuple[str, object]] = [("exact_identity", VehicleIdentity.id == identity.id)]
    if identity.generation:
        tiers.append(
            (
                "same_generation",
                (VehicleIdentity.brand == identity.brand)
                & (VehicleIdentity.model == identity.model)
                & (VehicleIdentity.generation == identity.generation),
            )
        )
    tiers.append(
        ("same_model", (VehicleIdentity.brand == identity.brand) & (VehicleIdentity.model == identity.model))
    )

    for tier_name, condition in tiers:
        remaining = target_count - len(result.comparables)
        if remaining <= 0:
            break

        candidates = (
            db.query(Listing)
            .join(VehicleIdentity, Listing.identity_id == VehicleIdentity.id)
            .filter(Listing.price_eur.isnot(None))
            .filter(condition)
            .all()
        )
        candidates = [c for c in candidates if c.id not in seen_ids]
        candidates.sort(key=lambda c: _distance(listing, c))
        chosen = candidates[:remaining]

        for candidate in chosen:
            seen_ids.add(candidate.id)
            result.comparables.append(
                Comparable(listing=candidate, tier=tier_name, delta_description=_delta_description(listing, candidate))
            )
        if chosen:
            result.tier_counts[tier_name] = len(chosen)

    return result
