"""Tests for the chat-agent tool wrappers in app/agent/tools.py.

The query logic lives in the services (covered in test_services.py); these tests cover the
tool-specific layer: the `.invoke({...})` boundary the agent uses and the serialization of
ORM rows into the agent-facing Pydantic read models. The tools open their own sessions via
the module-level SessionLocal, so each test points that at a fresh in-memory DB.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent import tools
from app.db.models import (
    Analysis,
    Base,
    BuyerCriteriaProfile,
    CriteriaAssessment,
    KnowledgeEntry,
    Listing,
    VehicleIdentity,
)


@pytest.fixture
def tools_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(tools, "SessionLocal", Session)
    return Session()


def test_get_listings_serializes_rows(tools_db):
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5", price_eur=15000)
    tools_db.add(listing)
    tools_db.commit()

    result = tools.get_listings.invoke({})

    assert len(result) == 1
    assert isinstance(result[0], tools.ListingRead)
    assert result[0].title == "VW T5"
    assert result[0].price_eur == 15000


def test_get_full_listing_includes_latest_analysis(tools_db):
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5", attributes={"tuev": "2026"})
    tools_db.add(listing)
    tools_db.commit()
    tools_db.add(
        Analysis(
            listing_id=listing.id,
            overall_score=88,
            tier="fair",
            confidence="medium",
            reasoning_text="reasoning",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    tools_db.commit()

    result = tools.get_full_listing.invoke({"listing_id": listing.id})

    assert isinstance(result, tools.ListingFullRead)
    assert result.attributes == {"tuev": "2026"}
    assert result.analysis.overall_score == 88


def test_get_full_listing_with_partial_analysis(tools_db):
    # An analysis whose nullable columns (score/tier/confidence/reasoning) are unset must
    # still serialize — AnalysisRead marks those Optional to match the model.
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5")
    tools_db.add(listing)
    tools_db.commit()
    tools_db.add(
        Analysis(listing_id=listing.id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    tools_db.commit()

    result = tools.get_full_listing.invoke({"listing_id": listing.id})

    assert result.analysis is not None
    assert result.analysis.overall_score is None
    assert result.analysis.tier is None


def test_get_full_listing_without_analysis(tools_db):
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5")
    tools_db.add(listing)
    tools_db.commit()

    result = tools.get_full_listing.invoke({"listing_id": listing.id})

    assert result.analysis is None


def test_get_full_listing_unknown_id(tools_db):
    assert tools.get_full_listing.invoke({"listing_id": 999}) is None


def test_get_listing_knowledge_serializes_entries(tools_db):
    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5")
    tools_db.add(identity)
    tools_db.commit()
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5", identity_id=identity.id)
    tools_db.add(listing)
    tools_db.add(
        KnowledgeEntry(
            identity_id=identity.id,
            entry_type="known_issue",
            payload={"summary": "dmf"},
            source_url="https://forum",
            confidence=0.7,
        )
    )
    tools_db.commit()

    result = tools.get_listing_knowledge.invoke({"listing_id": listing.id})

    assert len(result) == 1
    assert isinstance(result[0], tools.KnowledgeEntryRead)
    assert result[0].payload == {"summary": "dmf"}


def test_get_listing_knowledge_empty(tools_db):
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5", identity_id=None)
    tools_db.add(listing)
    tools_db.commit()

    assert tools.get_listing_knowledge.invoke({"listing_id": listing.id}) == []


# --- Buyer-criteria tools ----------------------------------------------------------


def a_profile(db):
    profile = BuyerCriteriaProfile(
        slug="boat",
        name="Boat towing",
        description="Judge the vehicle as a tow car.",
        free_text="I need to tow a 1200 kg boat trailer.",
        flags={"min_tow_capacity_kg": 1200},
        aspects=[{"key": "tow_hitch", "label": "Tow hitch", "prompt": "Is one fitted?"}],
    )
    db.add(profile)
    db.commit()
    return profile


def test_get_criteria_profiles_serializes_what_the_buyer_wants(tools_db):
    a_profile(tools_db)

    result = tools.get_criteria_profiles.invoke({})

    assert len(result) == 1
    assert isinstance(result[0], tools.CriteriaProfileRead)
    # The agent needs the buyer's own words and the rated aspects to explain a verdict.
    assert result[0].free_text == "I need to tow a 1200 kg boat trailer."
    assert result[0].flags == {"min_tow_capacity_kg": 1200}
    assert result[0].aspects[0]["key"] == "tow_hitch"


def test_get_criteria_profiles_empty(tools_db):
    assert tools.get_criteria_profiles.invoke({}) == []


def test_get_listing_criteria_assessment_returns_per_requirement_findings(tools_db):
    profile = a_profile(tools_db)
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5")
    tools_db.add(listing)
    tools_db.commit()
    analysis = Analysis(
        listing_id=listing.id,
        criteria_profile_id=profile.id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    tools_db.add(analysis)
    tools_db.commit()
    findings = {
        "findings": [
            {
                "aspect": "tow_hitch",
                "verdict": "meets",
                "severity": None,
                "description": "Tow hitch already fitted.",
                "supporting_quote": "Anhängerkupplung verbaut",
            }
        ],
        "positive_signals": [],
        "summary": "Usable as a tow car.",
    }
    tools_db.add(
        CriteriaAssessment(
            listing_id=listing.id,
            profile_id=profile.id,
            analysis_id=analysis.id,
            findings=findings,
        )
    )
    tools_db.commit()

    result = tools.get_listing_criteria_assessment.invoke({"listing_id": listing.id})

    assert isinstance(result, tools.CriteriaAssessmentRead)
    assert result.profile_id == profile.id
    assert result.findings["findings"][0]["verdict"] == "meets"


def test_get_listing_criteria_assessment_is_none_for_general_purpose_verdicts(tools_db):
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5")
    tools_db.add(listing)
    tools_db.commit()
    tools_db.add(
        Analysis(listing_id=listing.id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    )
    tools_db.commit()

    assert tools.get_listing_criteria_assessment.invoke({"listing_id": listing.id}) is None


def test_get_listing_criteria_assessment_unknown_listing(tools_db):
    assert tools.get_listing_criteria_assessment.invoke({"listing_id": 999}) is None


def test_full_listing_exposes_which_criteria_a_verdict_used(tools_db):
    """Without this the agent can't tell a general-purpose verdict from a criteria one."""
    profile = a_profile(tools_db)
    listing = Listing(kleinanzeigen_id="a", url="https://x", title="VW T5")
    tools_db.add(listing)
    tools_db.commit()
    tools_db.add(
        Analysis(
            listing_id=listing.id,
            criteria_profile_id=profile.id,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    tools_db.commit()

    result = tools.get_full_listing.invoke({"listing_id": listing.id})

    assert result.analysis.criteria_profile_id == profile.id
