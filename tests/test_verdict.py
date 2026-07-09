from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.comparables import Comparable, ComparablesResult
from app.analysis.condition import ConditionAnalysis, ConditionFinding
from app.analysis.judgment import AxisRating, Judgment
from app.analysis.reliability_score import ReliabilityRisk
from app.analysis.pipeline import run_full_analysis
from app.analysis.verdict import build_verdict
from app.db.models import Analysis, Base, KnowledgeEntry, Listing, VehicleIdentity
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
    condition → judgment) each get the right shape regardless of exact ordering."""

    def __init__(self, responses: list):
        self._responses = list(responses)

    def structured_completion(self, *, purpose, system, user, response_model, model):
        for i, r in enumerate(self._responses):
            if isinstance(r, response_model):
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
