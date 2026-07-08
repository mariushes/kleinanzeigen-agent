"""Deterministic model-reliability risk from collected knowledge entries.

One of two reliability signals fed to the verdict (the other is the LLM's assessment in
`condition.py`); both are surfaced so their usefulness can be compared across listings.
This one is a transparent, testable rule over the structured fields the extraction step
records (`severity`, `onset_km`, `stance`), scaled by how well the knowledge base matches
the listing's identity. It intentionally uses *this listing's mileage*: a catastrophic
fault that hits at 120k km matters far more for a 314k km van than a 90k km one.

Degrades gracefully: entries collected before the structured fields existed count as
"unknown-severity problems present" rather than being ignored, so an un-refreshed KB
still nudges the score while the UI hints that a re-collect would sharpen it.

Symmetric by design (user decision): positive knowledge — `strength` entries and
positive `overall_assessment`s — earns bonus points that offset problem penalties, since
old vehicles accumulate breakage reports regardless of whether they're dependable for
their class; a purely penalty-based rule would ratchet every aging model to "severe".
"""

from dataclasses import dataclass, field
from typing import Literal

from app.db.models import KnowledgeEntry

RiskLevel = Literal["none", "low", "moderate", "high", "severe"]

_SEVERITY_RANK = {"minor": 1, "moderate": 2, "major": 3, "catastrophic": 4}
_RANK_SEVERITY = {v: k for k, v in _SEVERITY_RANK.items()}

RISK_PENALTY = {"none": 0, "low": 5, "moderate": 12, "high": 22, "severe": 32}
TIER_FACTOR = {"exact_identity": 1.0, "same_generation": 0.8, "same_model": 0.6}

_STRENGTH_BONUS = 3
_MAX_STRENGTH_BONUS = 9
_POSITIVE_OVERALL_BONUS = 6
_NEGATIVE_OVERALL_PENALTY = 6


def penalty_for(level: str, tier: str | None, fallback_factor: float = 1.0) -> int:
    """Score penalty for a risk level, scaled by KB match tier. `fallback_factor` is used
    when there is no tier (e.g. the LLM assessed risk from its own knowledge, no KB)."""
    factor = TIER_FACTOR.get(tier or "", fallback_factor) if tier else fallback_factor
    return round(RISK_PENALTY.get(level, 0) * factor)


@dataclass
class ReliabilityRisk:
    level: RiskLevel
    penalty: int
    bonus: int = 0
    drivers: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    has_unrated_entries: bool = False

    @property
    def net_penalty(self) -> int:
        """Symmetric net effect on the score: problems subtract, strengths add back."""
        return self.penalty - self.bonus


def _km(value) -> int | None:
    return value if isinstance(value, int) else None


def assess_reliability_risk(
    entries: list[KnowledgeEntry],
    tier: str | None,
    listing_mileage_km: int | None,
) -> ReliabilityRisk:
    if not entries:
        return ReliabilityRisk(level="none", penalty=0)

    drivers: list[str] = []
    positives: list[str] = []
    worst_rank = 0
    has_unrated = False
    mileage_triggered = False
    unfavorable_variant = False
    strength_count = 0
    positive_overall = False
    negative_overall = False

    for entry in entries:
        payload = entry.payload or {}
        if entry.entry_type == "common_problem":
            severity = payload.get("severity")
            rank = _SEVERITY_RANK.get(severity, 0)
            if rank == 0:
                has_unrated = True
                rank = _SEVERITY_RANK["moderate"]  # unknown-but-present → assume moderate
            worst_rank = max(worst_rank, rank)

            onset = _km(payload.get("onset_km"))
            past_onset = onset is not None and listing_mileage_km is not None and listing_mileage_km >= onset
            if rank >= _SEVERITY_RANK["major"]:
                label = f"{_RANK_SEVERITY.get(rank, 'problem')}: {payload.get('component', 'issue')}"
                if past_onset:
                    mileage_triggered = True
                    label += f" (typically from {onset:,} km; this listing at {listing_mileage_km:,} km)"
                drivers.append(label)

        elif entry.entry_type == "mileage_expectation":
            onset = _km(payload.get("onset_km"))
            if onset is not None and listing_mileage_km is not None and listing_mileage_km >= onset:
                mileage_triggered = True
                drivers.append(
                    f"past known trouble mileage ({onset:,} km; this listing at {listing_mileage_km:,} km)"
                )

        elif entry.entry_type == "config_advice":
            stance = payload.get("stance")
            if stance == "unfavorable":
                unfavorable_variant = True
                drivers.append(f"variant advised against: {payload.get('component', 'this variant')}")
            elif stance == "favorable":
                strength_count += 1
                positives.append(f"variant endorsed: {payload.get('component', 'this variant')}")

        elif entry.entry_type == "strength":
            strength_count += 1
            positives.append(f"strength: {payload.get('component', 'trait')}")

        elif entry.entry_type == "overall_assessment":
            sentiment = payload.get("sentiment")
            if sentiment == "positive":
                positive_overall = True
                positives.append("overall reputation: positive")
            elif sentiment == "negative":
                negative_overall = True
                drivers.append("overall reputation: negative")

    level = _derive_level(worst_rank, mileage_triggered, unfavorable_variant)
    tier_factor = TIER_FACTOR.get(tier or "", 0.6)
    penalty = RISK_PENALTY[level] + (_NEGATIVE_OVERALL_PENALTY if negative_overall else 0)
    bonus = min(strength_count * _STRENGTH_BONUS, _MAX_STRENGTH_BONUS) + (
        _POSITIVE_OVERALL_BONUS if positive_overall else 0
    )
    return ReliabilityRisk(
        level=level,
        penalty=round(penalty * tier_factor),
        bonus=round(bonus * tier_factor),
        drivers=drivers,
        positives=positives,
        has_unrated_entries=has_unrated,
    )


def _derive_level(worst_rank: int, mileage_triggered: bool, unfavorable_variant: bool) -> RiskLevel:
    triggered = mileage_triggered or unfavorable_variant
    catastrophic = _SEVERITY_RANK["catastrophic"]
    major = _SEVERITY_RANK["major"]
    moderate = _SEVERITY_RANK["moderate"]

    if worst_rank >= catastrophic:
        return "severe" if triggered else "high"
    if worst_rank >= major:
        return "high" if triggered else "moderate"
    if worst_rank >= moderate:
        return "moderate" if mileage_triggered else "low"
    if worst_rank >= 1:
        return "low"
    # No rated problems at all, but a variant was explicitly advised against.
    return "moderate" if unfavorable_variant else "none"
