"""Formatting of retrieved comparables for the holistic price judgment.

Pricing is no longer a standalone LLM call: price fairness is judged inside the holistic
`judgment.py` call alongside condition and reliability, so the model can weigh a thin
comparable set against the ad's own condition rather than scoring price in isolation.

What remains here is the comparable-formatting helper — turning `comparables.py`'s tiered
results into the annotated, delta-labelled block the judgment prompt reads. Kept separate
so the retrieval/annotation logic stays testable on its own.

Deliberately not a statistical percentile/median: vehicle condition varies too much for
that to mean anything (a rough high-mileage van isn't comparable to a pristine one at the
same mileage). The comparables are handed over with human-readable deltas so the LLM can
reason about fairness given those *specific* differences.
"""

from app.analysis.comparables import ComparablesResult

PRICE_TIER_GUIDANCE = """\
Comparables are grouped by match tier:
- "exact_identity": same brand/model/generation/engine variant — most reliable anchor
- "same_generation": same brand/model/generation, different engine/trim — decent anchor
- "same_model": same brand/model only, possibly different generation/engine — weakest \
anchor, treat with caution
Do not just average the comparable prices — a comparable in much rougher condition or a \
worse-match tier should count for less."""


def format_comparables(comparables: ComparablesResult, forum_price_points: list[dict] | None = None) -> str:
    """Human-readable, tier-grouped comparable block for the judgment prompt. Returns an
    empty string when there is genuinely nothing to compare against (caller treats this as
    'no price data')."""
    lines: list[str] = []
    for comparable in comparables.comparables:
        c = comparable.listing
        lines.append(
            f'- [{comparable.tier}] "{c.title}" — {c.price_eur} EUR ({comparable.delta_description})'
        )

    if forum_price_points:
        lines.append("Forum-mentioned price points (secondary, less verified signal):")
        for point in forum_price_points:
            lines.append(
                f"- {point.get('price_eur')} EUR, {point.get('context', '')} (source: {point.get('source_url')})"
            )

    return "\n".join(lines)
