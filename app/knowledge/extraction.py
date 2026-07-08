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
affected engine/years, and any repair-cost figures in `detail`. Also set `severity` \
(how bad the consequence is: minor = annoyance/cheap; moderate = notable repair; major \
= expensive repair; catastrophic = engine/gearbox destruction or write-off risk) and, \
if the text gives a mileage at which it typically starts, `onset_km`.
- mileage_expectation: what mileage is normal/high/concerning, or at what mileage major \
work is typically due. `component` = the subsystem or "overall". If the text names a \
mileage beyond which serious trouble is likely, set `onset_km`.
- config_advice: which engine/trim/gearbox variants to prefer or avoid and why. \
`component` = the variant the advice is about (e.g. "180 PS biturbo", "DSG gearbox"). \
Set `stance_for_this_vehicle` to whether the advice makes THE VEHICLE BEING RESEARCHED \
(named at the top of the text) a good or bad choice: "unfavorable" if it advises against \
this vehicle's variant (or recommends a *different* variant over it), "favorable" if it \
endorses this vehicle's variant, "neutral" otherwise.
- price_point: a concrete real-world price mentioned. `component` = "price"; put the \
amount plus its mileage/year/condition context in `detail`.
- strength: something owners/mechanics PRAISE about this configuration — a component or \
trait regarded as robust/durable ("gearbox considered indestructible", "engine routinely \
exceeds 400,000 km with maintenance"). `component` = the praised part or trait.
- overall_assessment: a holistic verdict on this configuration's reliability reputation \
for its class and age ("solid van, weak engine choice", "among the most dependable \
transporters"). `component` = "overall". Set `sentiment` to positive / mixed / negative.

Old vehicles always accumulate breakage reports — that alone doesn't make a model bad. \
Extract the balance the text actually supports: if sources say problems are rare, \
maintenance-dependent, or the model is dependable *for its age/class*, capture that as \
strength/overall_assessment entries, not just the problems.

`component` is a short lowercase-ish key used to merge duplicate facts across sources, \
so keep it terse and consistent (the part or topic, not a sentence). `detail` is the \
full human-readable fact. `supporting_quote` is a short verbatim snippet backing it, if \
available. Leave `severity`, `onset_km`, `stance_for_this_vehicle`, and `sentiment` null \
when they don't apply to the entry type or aren't supported by the text.

Extract the most important 3-10 facts. Don't pad with trivia; skip anything not clearly \
about this vehicle's reliability, mileage, configuration, or pricing.
"""


class ExtractedEntry(BaseModel):
    type: Literal[
        "common_problem",
        "mileage_expectation",
        "config_advice",
        "price_point",
        "strength",
        "overall_assessment",
    ]
    component: str
    detail: str
    supporting_quote: str | None = None
    # Reliability-scoring signals (nullable; only some apply per type — see prompt).
    severity: Literal["minor", "moderate", "major", "catastrophic"] | None = None
    onset_km: int | None = None
    stance_for_this_vehicle: Literal["favorable", "neutral", "unfavorable"] | None = None
    sentiment: Literal["positive", "mixed", "negative"] | None = None


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
