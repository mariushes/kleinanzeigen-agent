from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.condition import ConditionAnalysis, ConditionFinding
from app.analysis.pricing import FairPriceRange, PriceVerdict
from app.analysis.verdict import combine_verdict, run_full_analysis
from app.db.models import Analysis, Base, Listing, VehicleIdentity
from app.knowledge.retrieval import ReliabilitySummary
from app.llm.provider import LLMCallResult
from app.vehicles.identity import ExtractedIdentity


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def clean_condition():
    return ConditionAnalysis(findings=[], positive_signals=["Scheckheftgepflegt", "TÜV neu"], summary="Clean listing.")


def fair_price():
    return PriceVerdict(
        tier="fair", fair_price_range=FairPriceRange(low_eur=9000, high_eur=11000),
        reasoning="In line with comparables.", confidence="high",
    )


def test_combine_verdict_clean_and_fair_scores_high_but_confidence_low_without_kb():
    verdict = combine_verdict(clean_condition(), fair_price(), ReliabilitySummary())

    assert verdict.tier == "buy_candidate"
    assert verdict.overall_score >= 70
    # Even a confident price verdict shouldn't read as high overall confidence with zero KB coverage.
    assert verdict.confidence == "low"


def test_combine_verdict_confidence_is_high_with_exact_identity_kb_and_confident_price():
    verdict = combine_verdict(clean_condition(), fair_price(), ReliabilitySummary(tier="exact_identity"))

    assert verdict.confidence == "high"


def test_combine_verdict_penalizes_high_severity_findings_and_overpriced():
    condition = ConditionAnalysis(
        findings=[
            ConditionFinding(category="project_car", severity="high", description="not running"),
            ConditionFinding(category="accident_history", severity="high", description="warped body"),
        ],
        positive_signals=[],
        summary="Parts car, not roadworthy.",
    )
    price = PriceVerdict(tier="overpriced", fair_price_range=None, reasoning="Too expensive for a parts car.", confidence="medium")

    verdict = combine_verdict(condition, price, ReliabilitySummary())

    assert verdict.tier == "avoid"
    assert verdict.overall_score < 45


def test_combine_verdict_reasoning_includes_all_three_sections():
    verdict = combine_verdict(clean_condition(), fair_price(), ReliabilitySummary())

    assert "Condition:" in verdict.reasoning
    assert "Price:" in verdict.reasoning
    assert "no knowledge base coverage yet" in verdict.reasoning


class FakeProvider:
    def __init__(self, responses: list):
        self._responses = iter(responses)

    def structured_completion(self, *, purpose, system, user, response_model, model):
        return LLMCallResult(parsed=next(self._responses), model=model, purpose=purpose, input_tokens=10, output_tokens=5)


def test_run_full_analysis_persists_analysis_row_and_sets_identity():
    db = make_db()
    listing = Listing(kleinanzeigen_id="1", url="https://x", title="VW T5 Multivan", attributes={})
    db.add(listing)
    db.commit()

    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
        clean_condition(),
    ])

    analysis = run_full_analysis(db, provider, listing)

    assert listing.identity_id is not None
    assert db.query(Analysis).count() == 1
    assert analysis.tier in {"buy_candidate", "caution", "avoid"}
    assert analysis.llm_model is not None
    assert analysis.price["tier"] == "insufficient_data"  # no comparables exist yet


def test_run_full_analysis_uses_comparables_for_price_when_available():
    db = make_db()
    identity = VehicleIdentity(brand="Volkswagen", model="T5 Multivan", canonical_label="Volkswagen | T5 Multivan")
    db.add(identity)
    db.commit()

    other = Listing(
        kleinanzeigen_id="other", url="https://x", title="Comparable van", price_eur=9500,
        mileage_km=100000, year=2015, attributes={}, identity_id=identity.id,
    )
    target = Listing(
        kleinanzeigen_id="target", url="https://x", title="Target van", price_eur=10000,
        mileage_km=105000, year=2015, attributes={}, identity_id=identity.id,
    )
    db.add_all([other, target])
    db.commit()

    provider = FakeProvider([
        clean_condition(),
        fair_price(),
    ])

    analysis = run_full_analysis(db, provider, target)

    assert analysis.price["tier"] == "fair"
    assert analysis.overall_score >= 70
