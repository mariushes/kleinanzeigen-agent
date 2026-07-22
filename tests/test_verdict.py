from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.comparables import Comparable, ComparablesResult
from app.analysis.condition import ConditionAnalysis, ConditionFinding
from app.analysis.criteria import CriteriaAnalysis, CriteriaFinding
from app.analysis.judgment import AxisRating, Judgment, JudgmentWithCriteria
from app.analysis.reliability_score import ReliabilityRisk
from app.analysis.pipeline import run_full_analysis
from app.analysis.verdict import build_verdict
from app.db.models import (
    Analysis,
    Base,
    BuyerCriteriaProfile,
    CriteriaAssessment,
    KnowledgeEntry,
    Listing,
    VehicleIdentity,
)
from app.knowledge.retrieval import ReliabilitySummary
from app.llm.provider import LLMCallResult
from app.vehicles.identity import ExtractedIdentity


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def a_judgment(score=72, rec="buy_candidate"):
    return Judgment(
        overall_score=score,
        recommendation=rec,
        price=AxisRating(rating="fair", note="in line"),
        condition=AxisRating(rating="good", note="clean"),
        reliability=AxisRating(rating="good", note="dependable"),
        positives=AxisRating(rating="good", note="documented"),
        reasoning="Solid.",
    )


def a_comparable(db):
    c = Listing(kleinanzeigen_id="c1", url="https://x", title="Comparable van", price_eur=9500)
    db.add(c)
    db.commit()
    return ComparablesResult(
        comparables=[Comparable(listing=c, tier="exact_identity", delta_description="+5,000 km")],
        tier_counts={"exact_identity": 1},
    )


def no_det_risk():
    return ReliabilityRisk(level="none", penalty=0)


def test_score_and_recommendation_come_straight_from_the_judgment():
    verdict = build_verdict(a_judgment(score=81), ComparablesResult(), ReliabilitySummary(), no_det_risk())
    assert verdict.overall_score == 81
    assert verdict.tier == "buy_candidate"


def test_axes_marked_no_data_when_evidence_absent():
    # No comparables and empty KB → price and reliability read "no_data", not their LLM rating.
    verdict = build_verdict(a_judgment(), ComparablesResult(), ReliabilitySummary(), no_det_risk())
    b = verdict.verdict_axes
    assert b["price"]["rating"] == "no_data"
    assert b["price"]["has_data"] is False
    assert b["reliability"]["rating"] == "no_data"
    # Condition and positives are always about this ad, so they always have data.
    assert b["condition"]["rating"] == "good"
    assert b["condition"]["has_data"] is True
    assert b["positives"]["rating"] == "good"


def test_axes_keep_llm_rating_when_evidence_present():
    db = make_db()
    reliability = ReliabilitySummary(
        entries=[KnowledgeEntry(entry_type="strength", payload={}, source_url="https://x")],
        tier="exact_identity",
    )
    verdict = build_verdict(a_judgment(), a_comparable(db), reliability, no_det_risk())
    b = verdict.verdict_axes
    assert b["price"]["rating"] == "fair"
    assert b["price"]["has_data"] is True
    assert b["reliability"]["rating"] == "good"
    assert b["reliability"]["has_data"] is True


def test_confidence_low_without_kb_even_with_comparables():
    db = make_db()
    verdict = build_verdict(a_judgment(), a_comparable(db), ReliabilitySummary(), no_det_risk())
    # Comparables present but zero KB coverage → floored to low.
    assert verdict.confidence == "low"


def test_confidence_high_with_comparables_and_exact_identity_kb():
    db = make_db()
    reliability = ReliabilitySummary(
        entries=[KnowledgeEntry(entry_type="strength", payload={}, source_url="https://x")],
        tier="exact_identity",
    )
    verdict = build_verdict(a_judgment(), a_comparable(db), reliability, no_det_risk())
    assert verdict.confidence == "high"


def test_reliability_deterministic_read_is_preserved_for_evidence_display():
    det = ReliabilityRisk(level="severe", penalty=32, drivers=["catastrophic: EGR cooler"])
    verdict = build_verdict(a_judgment(), ComparablesResult(), ReliabilitySummary(tier="exact_identity"), det)
    assert verdict.reliability["deterministic"]["level"] == "severe"
    assert verdict.reliability["deterministic"]["drivers"] == ["catastrophic: EGR cooler"]


class FakeProvider:
    """Serves queued responses by type so the pipeline's ordered calls (identity →
    condition → criteria → judgment) each get the right shape regardless of exact ordering.

    Matching is on the *exact* type, not `isinstance`: `JudgmentWithCriteria` subclasses
    `Judgment`, so a subclass check would let a plain `Judgment` satisfy a request for the
    criteria-carrying schema and quietly hide a wiring bug.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        for i, r in enumerate(self._responses):
            if type(r) is response_model:
                parsed = self._responses.pop(i)
                return LLMCallResult(parsed=parsed, model=model, purpose=purpose, input_tokens=10, output_tokens=5)
        raise AssertionError(f"no queued response of type {response_model.__name__}")


def clean_condition():
    return ConditionAnalysis(findings=[], positive_signals=["Scheckheftgepflegt"], summary="Clean.")


def test_run_full_analysis_persists_row_sets_identity_and_breakdown():
    db = make_db()
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5 Multivan", attributes={})
    db.add(listing)
    db.commit()

    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
        clean_condition(),
        a_judgment(),
    ])

    analysis = run_full_analysis(db, provider, listing)

    assert listing.identity_id is not None
    assert db.query(Analysis).count() == 1
    assert analysis.tier in {"buy_candidate", "caution", "avoid"}
    assert analysis.confidence in {"low", "medium", "high"}
    assert analysis.overall_score == 72
    # No comparables yet → price axis reads no_data.
    assert analysis.verdict_axes["price"]["rating"] == "no_data"
    assert analysis.reliability["deterministic"]["level"] == "none"  # empty KB


def test_run_full_analysis_deterministic_reliability_from_kb():
    db = make_db()
    identity = VehicleIdentity(brand="Volkswagen", model="T5", canonical_label="Volkswagen | T5 | 2.0 TDI 179")
    db.add(identity)
    db.commit()
    db.add(
        KnowledgeEntry(
            identity_id=identity.id, entry_type="common_problem",
            payload={"component": "EGR cooler", "detail": "catastrophic", "severity": "catastrophic", "onset_km": 120000},
            source_url="https://x", confidence=0.6,
        )
    )
    listing = Listing(
        kleinanzeigen_id="t", url="https://x", title="high miles", mileage_km=314000,
        attributes={}, identity_id=identity.id,
    )
    db.add(listing)
    db.commit()

    provider = FakeProvider([clean_condition(), a_judgment(score=40, rec="avoid")])
    analysis = run_full_analysis(db, provider, listing)

    # Deterministic read (catastrophic + past onset, exact tier → severe) is preserved as evidence.
    assert analysis.reliability["deterministic"]["level"] == "severe"
    # KB coverage exists → reliability axis carries the LLM rating, not no_data.
    assert analysis.verdict_axes["reliability"]["has_data"] is True


# --- Buyer-criteria axis -----------------------------------------------------------


def a_profile():
    """Shaped like a seeded profile but deliberately not the camper wording — the
    mechanism must work for any criteria set."""
    return BuyerCriteriaProfile(
        id=1,
        slug="boat",
        name="Boat towing",
        description="Judge the vehicle as a tow car.",
        free_text="I need to tow a 1200 kg boat trailer.",
        flags={},
        aspects=[{"key": "tow_hitch", "label": "Tow hitch", "prompt": "Is one fitted?"}],
    )


def a_criteria_judgment(rating="good", score=72):
    return JudgmentWithCriteria(
        **a_judgment(score=score).model_dump(),
        criteria=AxisRating(rating=rating, note="Tow hitch already fitted."),
    )


def judged_criteria(verdict="meets"):
    return CriteriaAnalysis(
        findings=[CriteriaFinding(aspect="tow_hitch", verdict=verdict, description="Fitted.")],
        positive_signals=[],
        summary="Fits.",
    )


def silent_criteria():
    return CriteriaAnalysis(
        findings=[CriteriaFinding(aspect="tow_hitch", verdict="unknown", description="Not stated.")],
        positive_signals=[],
        summary="The ad says nothing about towing.",
    )


def test_no_criteria_axis_without_a_profile():
    """The pre-criteria shape must be untouched when no profile is selected."""
    verdict = build_verdict(a_judgment(), ComparablesResult(), ReliabilitySummary(), no_det_risk())

    assert "criteria" not in verdict.verdict_axes
    assert set(verdict.verdict_axes) == {
        "overall_score", "price", "condition", "reliability", "positives",
    }


def test_criteria_axis_carries_rating_and_profile_label():
    verdict = build_verdict(
        a_criteria_judgment(), ComparablesResult(), ReliabilitySummary(), no_det_risk(),
        profile=a_profile(), criteria=judged_criteria(),
    )

    axis = verdict.verdict_axes["criteria"]
    assert axis["rating"] == "good"
    assert axis["has_data"] is True
    # The UI renders the axis name from the JSON, so it needs no profile lookup.
    assert axis["label"] == "Boat towing"
    assert axis["profile_slug"] == "boat"


def test_criteria_axis_is_no_data_when_the_ad_is_silent():
    """Silence about the requirements is an absence of evidence, not a bad fit."""
    verdict = build_verdict(
        a_criteria_judgment(rating="poor"), ComparablesResult(), ReliabilitySummary(), no_det_risk(),
        profile=a_profile(), criteria=silent_criteria(),
    )

    axis = verdict.verdict_axes["criteria"]
    assert axis["rating"] == "no_data"
    assert axis["has_data"] is False


def test_criteria_coverage_does_not_affect_confidence():
    """User decision: confidence stays floored on price + KB coverage only. An ad that is
    silent about the buyer's requirements must not read as a less confident verdict."""
    db = make_db()
    reliability = ReliabilitySummary(
        entries=[KnowledgeEntry(entry_type="strength", payload={}, source_url="https://x")],
        tier="exact_identity",
    )
    comparables = a_comparable(db)

    with_data = build_verdict(
        a_criteria_judgment(), comparables, reliability, no_det_risk(),
        profile=a_profile(), criteria=judged_criteria(),
    )
    without_data = build_verdict(
        a_criteria_judgment(), comparables, reliability, no_det_risk(),
        profile=a_profile(), criteria=silent_criteria(),
    )

    assert with_data.confidence == "high"
    assert without_data.confidence == "high"


def test_run_full_analysis_with_profile_stamps_and_persists_criteria():
    db = make_db()
    profile = a_profile()
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5", attributes={})
    db.add_all([profile, listing])
    db.commit()

    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5"),
        clean_condition(),
        judged_criteria(),
        a_criteria_judgment(),
    ])

    analysis = run_full_analysis(db, provider, listing, profile=profile)

    # The verdict records which criteria it was judged under.
    assert analysis.criteria_profile_id == profile.id
    assert analysis.verdict_axes["criteria"]["rating"] == "good"

    # Typed findings are persisted separately, like the structured condition analysis.
    assessment = db.query(CriteriaAssessment).one()
    assert assessment.listing_id == listing.id
    assert assessment.profile_id == profile.id
    assert assessment.analysis_id == analysis.id
    assert assessment.findings["findings"][0]["aspect"] == "tow_hitch"


def test_run_full_analysis_passes_the_buyers_requirements_into_the_judgment_prompt():
    db = make_db()
    profile = a_profile()
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5", attributes={})
    db.add_all([profile, listing])
    db.commit()

    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5"),
        clean_condition(),
        judged_criteria(),
        a_criteria_judgment(),
    ])
    run_full_analysis(db, provider, listing, profile=profile)

    judgment_call = next(c for c in provider.calls if c["purpose"] == "holistic_judgment")
    assert "I need to tow a 1200 kg boat trailer." in judgment_call["user"]
    assert "Tow hitch" in judgment_call["user"]
    assert "criteria:" in judgment_call["system"]


def test_run_full_analysis_without_profile_skips_the_criteria_call():
    db = make_db()
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5", attributes={})
    db.add(listing)
    db.commit()

    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5"),
        clean_condition(),
        a_judgment(),
    ])

    analysis = run_full_analysis(db, provider, listing)

    assert analysis.criteria_profile_id is None
    assert "criteria" not in analysis.verdict_axes
    assert db.query(CriteriaAssessment).count() == 0
    assert not any(c["purpose"] == "criteria_analysis" for c in provider.calls)
    # The unmodified system prompt must not mention the extra axis.
    judgment_call = next(c for c in provider.calls if c["purpose"] == "holistic_judgment")
    assert "criteria:" not in judgment_call["system"]
