from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.comparables import Comparable, ComparablesResult
from app.analysis.pricing import FairPriceRange, PriceVerdict, analyze_price
from app.db.models import Base, Listing, LlmCall
from app.llm.provider import LLMCallResult


class FakeProvider:
    def __init__(self, response: PriceVerdict):
        self._response = response
        self.called = False
        self.last_user_prompt: str | None = None

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.called = True
        self.last_user_prompt = user
        return LLMCallResult(parsed=self._response, model=model, purpose=purpose, input_tokens=15, output_tokens=6)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_listing(db, **kwargs) -> Listing:
    defaults = dict(kleinanzeigen_id="t", url="https://x", title="Target van", price_eur=10000, year=2015, mileage_km=150000)
    defaults.update(kwargs)
    listing = Listing(**defaults)
    db.add(listing)
    db.commit()
    return listing


def test_returns_insufficient_data_without_calling_llm_when_no_comparables():
    db = make_db()
    listing = make_listing(db)
    provider = FakeProvider(PriceVerdict(tier="fair", fair_price_range=None, reasoning="x", confidence="low"))

    verdict = analyze_price(db, provider, listing, "clean listing", ComparablesResult())

    assert verdict.tier == "insufficient_data"
    assert verdict.confidence == "low"
    assert provider.called is False
    assert db.query(LlmCall).count() == 0


def test_calls_llm_and_logs_when_comparables_present():
    db = make_db()
    listing = make_listing(db)
    comparable_listing = make_listing(db, kleinanzeigen_id="c1", title="Comparable van", price_eur=9500)
    comparables = ComparablesResult(
        comparables=[Comparable(listing=comparable_listing, tier="exact_identity", delta_description="+5,000 km")],
        tier_counts={"exact_identity": 1},
    )
    expected = PriceVerdict(
        tier="fair",
        fair_price_range=FairPriceRange(low_eur=9000, high_eur=11000),
        reasoning="In line with the one close comparable.",
        confidence="medium",
    )
    provider = FakeProvider(expected)

    verdict = analyze_price(db, provider, listing, "no red flags", comparables)

    assert verdict == expected
    assert provider.called is True
    assert "Comparable van" in provider.last_user_prompt
    assert "exact_identity" in provider.last_user_prompt
    assert db.query(LlmCall).count() == 1


def test_includes_forum_price_points_in_prompt_even_without_db_comparables():
    db = make_db()
    listing = make_listing(db)
    provider = FakeProvider(
        PriceVerdict(tier="fair", fair_price_range=None, reasoning="based on forum mentions", confidence="low")
    )

    verdict = analyze_price(
        db, provider, listing, "no red flags", ComparablesResult(),
        forum_price_points=[{"price_eur": 9000, "context": "similar engine, 140k km", "source_url": "https://forum/x"}],
    )

    assert provider.called is True
    assert "forum" in provider.last_user_prompt.lower()
    assert verdict.reasoning == "based on forum mentions"
