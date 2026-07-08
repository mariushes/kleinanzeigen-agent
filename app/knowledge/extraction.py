"""Turns a free-form `ResearchDocument` into typed `KnowledgeEntry` payloads.

This is the structured half of the two-call split (see CLAUDE.md): the grounded research
call already produced the text + citations; here a `response_schema` call distills it
into discrete, mergeable facts. No web access here — the model only reformats what the
document already says, so it can run on the cheap flash-lite model.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.knowledge.sources.base import ResearchDocument
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT = """\
You extract discrete, reusable reliability facts about a specific used-vehicle \
configuration from research text, for a prospective buyer. Output only facts actually \
supported by the given text — do not add general knowledge that isn't in it.

Each fact is one entry with a `type`:
- common_problem: a known fault/weak point. `component` = the affected part in a few \
words (e.g. "EGR cooler", "DMF/dual-mass flywheel", "timing chain"). Put the symptom, \
affected engine/years, and any repair-cost figures in `detail`.
- mileage_expectation: what mileage is normal/high/concerning for this vehicle, or at \
what mileage major work is typically due. `component` = the subsystem or "overall".
- config_advice: which engine/trim/gearbox variants to prefer or avoid and why. \
`component` = the variant the advice is about (e.g. "180 PS biturbo", "DSG gearbox").
- price_point: a concrete real-world price mentioned for this configuration. `component` \
= "price"; put the amount plus its mileage/year/condition context in `detail`.

`component` is a short lowercase-ish key used to merge duplicate facts across sources, \
so keep it terse and consistent (the part or topic, not a sentence). `detail` is the \
full human-readable fact. `supporting_quote` is a short verbatim snippet from the text \
that backs the fact, if available.

Extract the most important 3-8 facts. Don't pad with trivia; skip anything not clearly \
about this vehicle's reliability, mileage, configuration, or pricing.
"""


class ExtractedEntry(BaseModel):
    type: Literal["common_problem", "mileage_expectation", "config_advice", "price_point"]
    component: str
    detail: str
    supporting_quote: str | None = None


class ExtractionResult(BaseModel):
    entries: list[ExtractedEntry]


def extract_entries(
    db: Session,
    provider: LLMProvider,
    document: ResearchDocument,
    identity_label: str,
) -> list[ExtractedEntry]:
    settings = get_settings()
    user = (
        f"Vehicle: {identity_label}\n"
        f"Research topic: {document.title}\n\n"
        f"Research text:\n{document.text}"
    )
    result = provider.structured_completion(
        purpose="knowledge_extraction",
        system=_SYSTEM_PROMPT,
        user=user,
        response_model=ExtractionResult,
        model=settings.llm_model_fast,
    )
    record_llm_call(db, result, related_entity=f"knowledge:{identity_label[:80]}")
    return result.parsed.entries
