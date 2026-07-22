"""LLM assessment of one listing against a buyer-criteria profile.

The generic half of the buyer-criteria feature: this module knows *that* a profile has
free text, flags and aspects, never *what* they say. All the use-case wording (camper
conversions and whatever comes later) lives in `app/criteria/profiles/*.yaml`, so a new
criteria set is a new file, not a code branch.

Kept as its own call rather than folded into `condition.py` for the same reason condition
and reliability are separate: `condition.py`'s contract is "red flags in this ad's text",
and mixing "is this a good camper base" into it would double-count criteria evidence into
the condition axis. This call's findings feed the criteria axis only.

`unknown` is a first-class verdict here. Most ads say nothing about most requirements, and
a model that guesses is worse than useless — silence must read as "not stated" (which
`verdict.py` turns into a grey `no_data` axis), never as a failure.

Only the ad's *text* is read today. Conversion quality is often visible only in the photos
(`Listing.image_urls` is already scraped), so feeding images in here is the planned next
step — the output models below are deliberately independent of where the evidence came from.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import BuyerCriteriaProfile, Listing
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider

_SYSTEM_PROMPT_TEMPLATE = """\
You assess a used-vehicle classified ad against ONE buyer's specific requirements, for a \
buyer who doesn't know vehicles well. This is not a general condition review — judge only \
how well this vehicle serves the stated purpose below.

{buyer_requirements}

Rate each of these aspects, using its `key` verbatim in your `aspect` field:
{aspect_block}

Verdicts: `meets` = the ad shows this requirement is satisfied; `partial` = partly, or \
satisfiable with realistic work; `fails` = the ad shows this requirement is not met or is \
a serious problem; `unknown` = THE AD DOES NOT SAY. Use `unknown` freely and honestly — \
most ads are silent about most requirements, and a guess is worse than an admission. \
Never infer a conversion, an installation or a fitting that the ad does not mention. \
Severity applies to `fails`/`partial` only: high = walk away or budget a lot of work; \
medium = ask the seller; low = minor. Quote the ad (`supporting_quote`) whenever your \
verdict rests on something it actually says.

`positive_signals` must be SPECIFIC TO THE BUYER'S PURPOSE above — things that make this \
vehicle better *for that use*, which would not be worth mentioning to a buyer who wanted \
the vehicle for something else. The ad's general merits are assessed separately and must \
NOT be repeated here: no service history, TÜV/HU or inspection dates, recent maintenance \
(oil change, tyres, brakes, underbody treatment), number of owners, accident-free status, \
general condition, mileage, or standard factory equipment (air conditioning, parking \
sensors, tow bar, sat-nav). Include an item only if you can say which of the buyer's \
requirements it serves. If nothing qualifies, return an empty list — that is a normal \
outcome, not a gap to fill.

Finally, write a one-line `summary` of how well this vehicle fits the buyer's purpose.
"""


class CriteriaFinding(BaseModel):
    aspect: str
    verdict: Literal["meets", "partial", "fails", "unknown"]
    severity: Literal["low", "medium", "high"] | None = None
    description: str
    supporting_quote: str | None = None


class CriteriaAnalysis(BaseModel):
    findings: list[CriteriaFinding]
    positive_signals: list[str]
    summary: str

    @property
    def has_data(self) -> bool:
        """True when the ad actually said something about the buyer's requirements.

        All-`unknown` (or empty) means the ad is silent — the caller stamps the criteria
        axis `no_data` rather than letting an absence of evidence read as a bad fit.
        """
        return any(f.verdict != "unknown" for f in self.findings)


def build_system_prompt(profile: BuyerCriteriaProfile) -> str:
    """Assemble the system prompt from the profile row. No knowledge of any specific
    criteria set — `free_text`, `flags` and `aspects[].prompt` are injected as written."""
    requirement_lines = [f"The buyer's purpose: {profile.name}."]
    if profile.description:
        requirement_lines.append(profile.description)
    if profile.free_text:
        requirement_lines.append(f'In the buyer\'s own words: "{profile.free_text}"')
    if profile.flags:
        flags = ", ".join(f"{k}={v}" for k, v in profile.flags.items())
        requirement_lines.append(f"Stated preferences: {flags}")

    aspect_block = "\n".join(
        f"- {a['key']} ({a['label']}): {a['prompt']}" for a in (profile.aspects or [])
    )
    return _SYSTEM_PROMPT_TEMPLATE.format(
        buyer_requirements="\n".join(requirement_lines),
        aspect_block=aspect_block,
    )


def _build_user_prompt(listing: Listing) -> str:
    attrs = listing.attributes or {}
    return (
        f"Vehicle: {listing.identity.canonical_label if listing.identity else listing.title}\n"
        f"Title: {listing.title}\n"
        f"Price: {listing.price_eur} EUR\n"
        f"Year: {listing.year}, Mileage: {listing.mileage_km} km\n"
        f"Condition label: {attrs.get('condition_label')}\n"
        f"Features: {attrs.get('features')}\n"
        f"Raw details: {attrs.get('raw_details')}\n\n"
        f"Full description:\n{listing.description_text or ''}"
    )


def assess_criteria(
    db: Session,
    provider: LLMProvider,
    listing: Listing,
    profile: BuyerCriteriaProfile,
) -> CriteriaAnalysis:
    settings = get_settings()
    result = provider.structured_completion(
        purpose="criteria_analysis",
        system=build_system_prompt(profile),
        user=_build_user_prompt(listing),
        response_model=CriteriaAnalysis,
        model=settings.llm_model_quality,
    )
    record_llm_call(db, result, related_entity=f"listing:{listing.id}")
    return result.parsed
