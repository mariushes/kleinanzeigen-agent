"""Tiered-fallback retrieval of reliability knowledge for a vehicle identity.

Built ahead of the knowledge-collection pipeline (Milestone E) so `verdict.py` has a
stable interface to depend on: with an empty `knowledge_entries` table this always
returns "no coverage", and starts returning real data as soon as Milestone E populates
entries — no changes needed here when that lands.

Uses the same tiered exact_identity → same_generation → same_model pattern as
`app/analysis/comparables.py`, for the same reason: exact-identity matching alone
fragments too easily (see PLAN.md's note on near-duplicate engine variants).
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db.models import KnowledgeEntry, VehicleIdentity


@dataclass
class ReliabilitySummary:
    entries: list[KnowledgeEntry] = field(default_factory=list)
    tier: str | None = None

    @property
    def has_coverage(self) -> bool:
        return bool(self.entries)


def get_reliability_summary(db: Session, identity: VehicleIdentity | None) -> ReliabilitySummary:
    if identity is None:
        return ReliabilitySummary()

    tiers: list[tuple[str, object]] = [("exact_identity", KnowledgeEntry.identity_id == identity.id)]
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
        entries = (
            db.query(KnowledgeEntry)
            .join(VehicleIdentity, KnowledgeEntry.identity_id == VehicleIdentity.id)
            .filter(condition)
            .all()
        )
        if entries:
            return ReliabilitySummary(entries=entries, tier=tier_name)

    return ReliabilitySummary()


def format_for_prompt(summary: ReliabilitySummary) -> str:
    if not summary.has_coverage:
        return "No reliability knowledge base coverage yet for this vehicle configuration."

    lines = [f"Reliability knowledge ({summary.tier} match):"]
    for entry in summary.entries:
        lines.append(f"- [{entry.entry_type}] {entry.payload} (mentioned {entry.mention_count}x, source: {entry.source_url})")
    return "\n".join(lines)
