"""Capped, on-demand knowledge-collection job for one vehicle identity.

Flow per identity: pick research angles not yet covered for this identity → each angle's
query runs through the active `KnowledgeSource` (grounded web search) into
`ResearchDocument`s → each document is extracted into typed facts → facts are merged into
`knowledge_entries`, where a repeat of the same (identity, type, component) fact bumps a
mention counter and confidence instead of duplicating.

Progressive by design: a repeat "Refresh" run advances to angles not yet researched
(tracked in `knowledge_research_runs`) and tells the model which components are already
known, so it hunts for *new* facts rather than re-confirming the same ones. Everything is
capped (`max_queries`) so a run can't spiral through the free-tier quota. Brand-agnostic:
angles are phrased against the identity label, never hardcoded to a model.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db.models import KnowledgeEntry, KnowledgeResearchRun, VehicleIdentity
from app.knowledge.extraction import extract_entries
from app.knowledge.sources.base import KnowledgeSource
from app.llm.provider import LLMProvider

_CONFIDENCE_START = 0.5
_CONFIDENCE_STEP = 0.1
_CONFIDENCE_CAP = 0.95

# Ordered pool of distinct research angles. A collection run consumes the next uncovered
# angles for the identity, so successive runs broaden coverage instead of repeating.
RESEARCH_ANGLES: list[tuple[str, str]] = [
    ("common_problems", "common problems, known weak points, and reliability issues"),
    ("variants_mileage", "which engine and trim variants to buy or avoid, and what mileage is considered high or concerning"),
    ("engine_turbo", "engine, turbocharger, injector, EGR and timing chain/belt problems and their repair costs"),
    ("gearbox", "gearbox and clutch problems (manual, automatic, DSG) and known failure mileages"),
    ("rust_body", "rust, corrosion, body and chassis weak spots to inspect"),
    ("electrics", "electrical, electronics, sensor and comfort-feature faults"),
    ("running_costs", "typical maintenance intervals, running costs, and expensive scheduled jobs"),
    ("buyer_checklist", "what to check on a test drive and pre-purchase inspection for this model"),
]


@dataclass
class BuildResult:
    identity_label: str
    angles: list[str] = field(default_factory=list)
    queries_run: int = 0
    documents: int = 0
    created: int = 0
    merged: int = 0
    failed: list[str] = field(default_factory=list)


def _component_key(component: str) -> str:
    return re.sub(r"\s+", " ", component).strip().lower()


def select_angles(db: Session, identity: VehicleIdentity, max_queries: int) -> list[tuple[str, str]]:
    """Next uncovered angles for this identity; wraps to reinforce once all are covered."""
    covered = {
        r.angle_key
        for r in db.query(KnowledgeResearchRun).filter(
            KnowledgeResearchRun.identity_id == identity.id
        )
    }
    uncovered = [a for a in RESEARCH_ANGLES if a[0] not in covered]
    pool = uncovered or RESEARCH_ANGLES  # everything covered → start reinforcing from the top
    return pool[:max_queries]


_BILINGUAL_INSTRUCTION = (
    " Draw on both English and German-language owner forums, mechanics, and buying guides "
    "(including German sources such as motor-talk.de and model-specific German forums), "
    "since the richest reliability discussion for European models is often in German."
)


def _build_query(identity: VehicleIdentity, topic: str, known_components: list[str]) -> str:
    query = f"{identity.canonical_label}: {topic}"
    if known_components:
        # Steer the grounded search toward new ground rather than already-known facts.
        known = ", ".join(sorted(known_components)[:15])
        query += f". Focus on details beyond what is already known about: {known}"
    return query + _BILINGUAL_INSTRUCTION


def build_knowledge_for_identity(
    db: Session,
    provider: LLMProvider,
    source: KnowledgeSource,
    identity: VehicleIdentity,
    max_queries: int,
    max_documents_per_query: int = 1,
) -> BuildResult:
    result = BuildResult(identity_label=identity.canonical_label)

    known_components = sorted(
        {
            e.payload.get("component", "")
            for e in db.query(KnowledgeEntry).filter(KnowledgeEntry.identity_id == identity.id)
            if e.payload.get("component")
        }
    )

    for angle_key, topic in select_angles(db, identity, max_queries):
        result.angles.append(angle_key)
        result.queries_run += 1
        query = _build_query(identity, topic, known_components)
        try:
            documents = source.research(query, max_documents=max_documents_per_query)
        except Exception as exc:  # noqa: BLE001 - one bad query shouldn't abort the run
            result.failed.append(f"research '{angle_key}': {exc}")
            continue

        db.add(KnowledgeResearchRun(identity_id=identity.id, angle_key=angle_key))

        for document in documents:
            result.documents += 1
            first_citation = document.citations[0] if document.citations else None
            source_url = document.url or (first_citation.url if first_citation else source.name)
            # Grounding citation URLs are opaque vertexaisearch redirects; the citation
            # titles (e.g. "t6forum.com") are what's worth showing. For a grounded doc the
            # sources are doc-level, so we label the entry with all the domains behind it.
            source_label = (
                ", ".join(dict.fromkeys(c.title for c in document.citations if c.title))
                or document.title
            )
            try:
                extracted = extract_entries(db, provider, document, identity.canonical_label)
            except Exception as exc:  # noqa: BLE001
                result.failed.append(f"extract '{angle_key}': {exc}")
                continue

            # Merge against existing entries for this identity in Python — a single
            # identity's KB is small, and this avoids JSON-path SQL portability concerns.
            existing_by_key: dict[tuple[str, str], KnowledgeEntry] = {
                (e.entry_type, _component_key(e.payload.get("component", ""))): e
                for e in db.query(KnowledgeEntry).filter(KnowledgeEntry.identity_id == identity.id).all()
            }

            for entry in extracted:
                key = (entry.type, _component_key(entry.component))
                existing = existing_by_key.get(key)
                if existing:
                    existing.mention_count += 1
                    existing.confidence = min(_CONFIDENCE_CAP, existing.confidence + _CONFIDENCE_STEP)
                    result.merged += 1
                else:
                    new_entry = KnowledgeEntry(
                        identity_id=identity.id,
                        entry_type=entry.type,
                        payload={
                            "component": entry.component,
                            "detail": entry.detail,
                            "source_label": source_label,
                        },
                        source_url=source_url,
                        source_quote=entry.supporting_quote,
                        mention_count=1,
                        confidence=_CONFIDENCE_START,
                    )
                    db.add(new_entry)
                    # Register immediately so a duplicate later in the same document merges
                    # instead of inserting twice.
                    existing_by_key[key] = new_entry
                    result.created += 1

    db.commit()
    return result
