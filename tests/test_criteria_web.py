"""Web-layer tests for buyer-criteria selection and rendering.

The selection model under test: criteria are chosen on the *forms that trigger work*
(scan, re-analyze) and recorded on what those produce — there is no global "current
profile" state, so a verdict always renders under the criteria it was judged with.
"""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Analysis,
    Base,
    BuyerCriteriaProfile,
    CriteriaAssessment,
    Listing,
    SearchRun,
)
from app.db.session import get_db
from app.main import app

client = TestClient(app)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine)

    def _override():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestSessionLocal()
    app.dependency_overrides.pop(get_db, None)


def a_profile(db):
    profile = BuyerCriteriaProfile(
        slug="boat",
        name="Boat towing",
        description="Judge the vehicle as a tow car.",
        free_text="I need to tow a 1200 kg boat trailer.",
        flags={},
        aspects=[{"key": "tow_hitch", "label": "Tow hitch", "prompt": "Is one fitted?"}],
    )
    db.add(profile)
    db.commit()
    return profile


def a_listing(db):
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5", attributes={})
    db.add(listing)
    db.commit()
    return listing


def an_analysis(db, listing, profile=None, criteria_axis=None):
    analysis = Analysis(
        listing_id=listing.id,
        criteria_profile_id=profile.id if profile else None,
        condition={"findings": [], "positive_signals": [], "summary": "Clean."},
        price={},
        reliability={},
        verdict_axes={
            "overall_score": 72,
            "price": {"rating": "fair", "note": "in line", "has_data": True},
            "condition": {"rating": "good", "note": "clean", "has_data": True},
            "reliability": {"rating": "good", "note": "ok", "has_data": True},
            "positives": {"rating": "good", "note": "documented", "has_data": True},
            **({"criteria": criteria_axis} if criteria_axis else {}),
        },
        overall_score=72,
        tier="buy_candidate",
        confidence="high",
        reasoning_text="Solid.",
    )
    db.add(analysis)
    db.commit()
    return analysis


# --- Selection on the search form -------------------------------------------------


def test_dashboard_offers_the_profile_dropdown(db_session):
    a_profile(db_session)

    body = client.get("/").text

    assert 'name="criteria_profile_id"' in body
    assert "Boat towing" in body
    assert "General purpose (no special needs)" in body


def test_search_run_records_the_selected_profile(db_session):
    profile = a_profile(db_session)

    with patch("app.web.routes.runs.execute_search_run"):
        client.post(
            "/search-runs",
            data={
                "search_url": "https://kleinanzeigen.de/s-autos/k0",
                "max_listings": "5",
                "criteria_profile_id": str(profile.id),
            },
            follow_redirects=False,
        )

    run = db_session.query(SearchRun).one()
    assert run.criteria_profile_id == profile.id


def test_search_run_without_a_profile_is_general_purpose(db_session):
    a_profile(db_session)

    with patch("app.web.routes.runs.execute_search_run"):
        client.post(
            "/search-runs",
            data={
                "search_url": "https://kleinanzeigen.de/s-autos/k0",
                "max_listings": "5",
                "criteria_profile_id": "",
            },
            follow_redirects=False,
        )

    assert db_session.query(SearchRun).one().criteria_profile_id is None


def test_search_run_form_still_works_without_the_field(db_session):
    """The field is optional, so an older/simpler form post must not 422."""
    with patch("app.web.routes.runs.execute_search_run"):
        response = client.post(
            "/search-runs",
            data={"search_url": "https://kleinanzeigen.de/s-autos/k0", "max_listings": "5"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert db_session.query(SearchRun).one().criteria_profile_id is None


# --- Re-analyze keeps the verdict's own criteria ----------------------------------


def test_reanalyze_form_preselects_the_profile_the_verdict_used(db_session):
    profile = a_profile(db_session)
    listing = a_listing(db_session)
    an_analysis(db_session, listing, profile, {"rating": "good", "note": "fits", "has_data": True})

    body = client.get(f"/listings/{listing.id}").text

    # The verdict's own profile is the selected option, so a plain re-analyze keeps it.
    assert f'<option value="{profile.id}" selected>' in body.replace(" >", ">")


def test_reanalyze_passes_the_chosen_profile_to_the_job(db_session):
    profile = a_profile(db_session)
    listing = a_listing(db_session)

    with patch("app.web.routes.listings.execute_reanalyze") as job:
        client.post(
            f"/listings/{listing.id}/reanalyze",
            data={"criteria_profile_id": str(profile.id)},
            follow_redirects=False,
        )

    _, kwargs = job.call_args
    assert kwargs["criteria_profile_id"] == profile.id


def test_reanalyze_can_switch_back_to_general_purpose(db_session):
    a_profile(db_session)
    listing = a_listing(db_session)

    with patch("app.web.routes.listings.execute_reanalyze") as job:
        client.post(
            f"/listings/{listing.id}/reanalyze",
            data={"criteria_profile_id": ""},
            follow_redirects=False,
        )

    _, kwargs = job.call_args
    assert kwargs["criteria_profile_id"] is None


# --- Rendering ---------------------------------------------------------------------


def test_dashboard_shows_the_criteria_column_when_a_verdict_has_one(db_session):
    profile = a_profile(db_session)
    listing = a_listing(db_session)
    an_analysis(
        db_session, listing, profile,
        {"rating": "good", "note": "fits", "has_data": True, "label": "Boat towing"},
    )

    body = client.get("/").text

    assert "Boat towing" in body


def test_dashboard_has_no_criteria_column_for_general_purpose_verdicts(db_session):
    """A dashboard of criteria-free verdicts must look exactly as it did before."""
    listing = a_listing(db_session)
    an_analysis(db_session, listing)

    body = client.get("/").text

    assert "How well this vehicle serves your stated purpose" not in body


def test_detail_page_renders_the_criteria_axis_and_findings(db_session):
    profile = a_profile(db_session)
    listing = a_listing(db_session)
    analysis = an_analysis(
        db_session, listing, profile,
        {"rating": "good", "note": "Tow hitch fitted.", "has_data": True, "label": "Boat towing"},
    )
    db_session.add(
        CriteriaAssessment(
            listing_id=listing.id,
            profile_id=profile.id,
            analysis_id=analysis.id,
            findings={
                "findings": [
                    {
                        "aspect": "tow_hitch",
                        "verdict": "meets",
                        "severity": None,
                        "description": "Tow hitch already fitted.",
                        "supporting_quote": "Anhängerkupplung verbaut",
                    }
                ],
                "positive_signals": ["Tow hitch fitted"],
                "summary": "Usable as a tow car.",
            },
        )
    )
    db_session.commit()

    body = client.get(f"/listings/{listing.id}").text

    assert "judged for: Boat towing" in body
    assert "requirements fit" in body
    # The aspect renders under its human label, not its raw key.
    assert "Tow hitch" in body
    assert "Tow hitch already fitted." in body
    assert "Anhängerkupplung verbaut" in body


def test_detail_page_renders_unknown_aspects_as_not_stated(db_session):
    """Silence must not read as a failure anywhere in the UI."""
    profile = a_profile(db_session)
    listing = a_listing(db_session)
    analysis = an_analysis(
        db_session, listing, profile,
        {"rating": "no_data", "note": "Ad is silent.", "has_data": False, "label": "Boat towing"},
    )
    db_session.add(
        CriteriaAssessment(
            listing_id=listing.id,
            profile_id=profile.id,
            analysis_id=analysis.id,
            findings={
                "findings": [
                    {
                        "aspect": "tow_hitch",
                        "verdict": "unknown",
                        "severity": None,
                        "description": "The ad does not mention a tow hitch.",
                        "supporting_quote": None,
                    }
                ],
                "positive_signals": [],
                "summary": "The ad says nothing about towing.",
            },
        )
    )
    db_session.commit()

    body = client.get(f"/listings/{listing.id}").text

    # Assert on the rendered table cell specifically — the section's explanatory footnote
    # also contains the words "Not stated", so a bare substring check would pass even if
    # the cell wrongly rendered the raw "unknown".
    assert re.search(r'<td class="criteria-unknown">\s*Not stated', body)
    assert ">unknown<" not in body
    # ...and the axis chip reads as an absence of evidence, not a bad fit.
    assert "No data" in body


def test_detail_page_of_a_general_purpose_verdict_has_no_criteria_section(db_session):
    listing = a_listing(db_session)
    an_analysis(db_session, listing)

    body = client.get(f"/listings/{listing.id}").text

    assert "requirements fit" not in body
    assert "judged for:" not in body
