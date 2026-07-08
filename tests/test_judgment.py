from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.comparables import Comparable, ComparablesResult
from app.analysis.condition import ConditionAnalysis, ConditionFinding
from app.analysis.judgment import AxisRating, Judgment, judge_listing
from app.analysis.reliability_score import ReliabilityRisk
from app.db.models import Base, KnowledgeEntry, Listing, LlmCall, VehicleIdentity
from app.knowledge.retrieval import ReliabilitySummary
from app.llm.provider import LLMCallResult


class FakeProvider:
    def __init__(self, response: Judgment):
        self._response = response
        self.last_user_prompt: str | None = None

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.last_user_prompt = user
        return LLMCallResult(parsed=self._response, model=model, purpose=purpose, input_tokens=30, output_tokens=12)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def a_judgment():
    return Judgment(
        overall_score=72,
        recommendation="buy_candidate",
        price=AxisRating(rating="fair", note="in line with comparables"),
        condition=AxisRating(rating="good", note="clean, documented"),
        reliability=AxisRating(rating="good", note="dependable engine"),
        positives=AxisRating(rating="good", note="full service history"),
        reasoning="Solid buy.",
    )


def test_judge_listing_feeds_evidence_and_logs_call():
    db = make_db()
    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5 | 2.0 TDI")
    db.add(identity)
    db.commit()
    listing = Listing(
        kleinanzeigen_id="t", url="https://x", title="Target van", price_eur=10000,
        year=2015, mileage_km=150000, attributes={}, identity_id=identity.id,
    )
    comparable = Listing(kleinanzeigen_id="c1", url="https://x", title="Comparable van", price_eur=9500)
    db.add_all([listing, comparable])
    db.commit()

    condition = ConditionAnalysis(
        findings=[ConditionFinding(category="rust", severity="medium", description="sill rust", supporting_quote="Rost")],
        positive_signals=["Scheckheftgepflegt"],
        summary="Minor rust.",
    )
    comparables = ComparablesResult(
        comparables=[Comparable(listing=comparable, tier="exact_identity", delta_description="+5,000 km")],
        tier_counts={"exact_identity": 1},
    )
    reliability = ReliabilitySummary(
        entries=[KnowledgeEntry(identity_id=identity.id, entry_type="strength", payload={"component": "engine"}, source_url="https://x")],
        tier="exact_identity",
    )
    det = ReliabilityRisk(level="low", penalty=3, positives=["strength: engine"])

    provider = FakeProvider(a_judgment())
    result = judge_listing(db, provider, listing, condition, comparables, reliability, det)

    assert result.recommendation == "buy_candidate"
    # Evidence from every axis reaches the prompt.
    assert "Comparable van" in provider.last_user_prompt
    assert "sill rust" in provider.last_user_prompt
    assert "Scheckheftgepflegt" in provider.last_user_prompt
    assert "strength: engine" in provider.last_user_prompt
    assert db.query(LlmCall).count() == 1
    assert db.query(LlmCall).one().related_entity == f"listing:{listing.id}"
