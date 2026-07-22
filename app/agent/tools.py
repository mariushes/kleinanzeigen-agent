"""Chat-agent read tools.

Thin `@tool` wrappers: each opens a session and delegates the query to
`app/services/{listings,knowledge}.py` (session in, plain data out), then serializes the
ORM rows into agent-facing Pydantic read models. Query/shaping logic lives in the
services, not here, so a page and a chat tool read the same rows the same way.
"""

from langchain.tools import tool
from pydantic import BaseModel, ConfigDict

from app.db.session import SessionLocal
from app.services import criteria as criteria_service
from app.services import knowledge as knowledge_service
from app.services import listings as listings_service
from app.config import get_settings


class AnalysisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    listing_id: int

    condition: dict
    price: dict
    reliability: dict
    verdict_axes: dict
    overall_score: int | None
    tier: str | None
    confidence: str | None
    reasoning_text: str | None
    # Which buyer-criteria profile this verdict was judged under, if any. Without it the
    # agent can't tell a general-purpose verdict from one judged against special needs.
    criteria_profile_id: int | None = None


class CriteriaProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None
    free_text: str | None
    flags: dict
    aspects: list


class CriteriaAssessmentRead(BaseModel):
    """This ad's per-requirement assessment against a buyer-criteria profile."""

    model_config = ConfigDict(from_attributes=True)

    listing_id: int
    profile_id: int
    findings: dict


class ListingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    price_eur: int | None
    year: int | None
    mileage_km: int | None
    location: str | None
    seller_type: str | None
    url: str


class ListingFullRead(ListingRead):
    model_config = ConfigDict(from_attributes=True)
    attributes: dict
    analysis: AnalysisRead | None


class KnowledgeEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_type: str
    payload: dict
    source_url: str
    source_quote: str | None
    mention_count: int
    confidence: float


@tool
def get_listings() -> list[ListingRead]:
    """Get all listings."""
    with SessionLocal() as db:
        return [ListingRead.model_validate(r) for r in listings_service.list_listings(db)]


@tool
def get_full_listing(listing_id: int) -> ListingFullRead | None:
    """Get full listing including vehicle attributes and LLM-based analysis by ID."""
    with SessionLocal() as db:
        listing = listings_service.get_listing(db, listing_id)
        if listing is None:
            return None
        analysis = listings_service.latest_analysis(listing)
        listing_dict = dict(listing.__dict__)
        listing_dict["analysis"] = (
            AnalysisRead.model_validate(analysis) if analysis is not None else None
        )
        return ListingFullRead.model_validate(listing_dict)


@tool
def get_criteria_profiles() -> list[CriteriaProfileRead]:
    """Get the buyer-criteria profiles: the special needs a listing can be judged against.

    A profile describes what the buyer wants to use a vehicle for (in their own words, plus
    the specific aspects the analysis rates). Use this to find out what the buyer cares
    about before explaining or comparing verdicts.
    """
    with SessionLocal() as db:
        return [CriteriaProfileRead.model_validate(p) for p in criteria_service.list_profiles(db)]


@tool
def get_listing_criteria_assessment(listing_id: int) -> CriteriaAssessmentRead | None:
    """Get how a listing scored against the buyer's criteria, requirement by requirement.

    Returns the typed findings behind the verdict's criteria axis: for each requirement, a
    verdict of meets / partial / fails / unknown, plus the reasoning and any supporting
    quote from the ad. `unknown` means the ad is simply silent about that requirement — an
    open question to ask the seller, NOT a failing. Returns None if the listing was
    analyzed without any criteria profile.
    """
    with SessionLocal() as db:
        listing = listings_service.get_listing(db, listing_id)
        if listing is None:
            return None
        analysis = listings_service.latest_analysis(listing)
        assessment = criteria_service.get_assessment(db, analysis)
        if assessment is None:
            return None
        return CriteriaAssessmentRead.model_validate(assessment)


@tool
def get_listing_knowledge(listing_id: int) -> list[KnowledgeEntryRead]:
    """Get all knowledge base entries for a listing's vehicle model.

    Knowledge entries (known issues, reliability facts, etc.) are attached to the
    vehicle identity rather than the individual listing, so this resolves the
    listing's identity and returns that identity's entries, most confident first.
    Returns an empty list if the listing is unknown or has no identified vehicle.
    """
    with SessionLocal() as db:
        entries = knowledge_service.entries_for_listing(db, listing_id)
        return [KnowledgeEntryRead.model_validate(e) for e in entries]
