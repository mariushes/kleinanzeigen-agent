from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.condition import ConditionAnalysis, ConditionFinding, analyze_condition
from app.db.models import Base, Listing, LlmCall
from app.llm.provider import LLMCallResult


class FakeProvider:
    def __init__(self, response: ConditionAnalysis):
        self._response = response
        self.last_call_kwargs: dict | None = None

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.last_call_kwargs = {"purpose": purpose, "system": system, "user": user, "model": model}
        return LLMCallResult(
            parsed=self._response, model=model, purpose=purpose, input_tokens=20, output_tokens=8,
        )


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_analyze_condition_returns_parsed_findings_and_logs_call():
    db = make_db()
    listing = Listing(
        kleinanzeigen_id="1", url="https://x", title="VW T5 Bastlerfahrzeug",
        description_text="Nicht fahrbereit, Motorschaden, kein Scheckheft.",
        attributes={"condition_label": "Beschädigt", "features": [], "raw_details": {}},
    )
    db.add(listing)
    db.commit()

    expected = ConditionAnalysis(
        findings=[
            ConditionFinding(
                category="project_car", severity="high",
                description="Advertised as non-running project car",
                supporting_quote="Nicht fahrbereit",
            ),
            ConditionFinding(
                category="missing_service_history", severity="medium",
                description="No service booklet", supporting_quote="kein Scheckheft",
            ),
        ],
        positive_signals=[],
        summary="Significant risk: sold as a non-running project car with no service history.",
    )
    provider = FakeProvider(expected)

    result = analyze_condition(db, provider, listing)

    assert result == expected
    assert provider.last_call_kwargs["purpose"] == "condition_analysis"
    assert "VW T5 Bastlerfahrzeug" in provider.last_call_kwargs["user"]
    assert db.query(LlmCall).count() == 1
    call = db.query(LlmCall).one()
    assert call.related_entity == f"listing:{listing.id}"
    assert call.input_tokens == 20
    assert call.output_tokens == 8


def test_analyze_condition_allows_clean_listing_with_no_findings():
    db = make_db()
    listing = Listing(
        kleinanzeigen_id="2", url="https://x", title="VW T5 gepflegt",
        description_text="Scheckheftgepflegt, TÜV neu, Nichtraucherfahrzeug.",
        attributes={},
    )
    db.add(listing)
    db.commit()

    expected = ConditionAnalysis(
        findings=[], positive_signals=["Scheckheftgepflegt", "TÜV neu"],
        summary="Listing reads clean, no red flags found.",
    )
    provider = FakeProvider(expected)

    result = analyze_condition(db, provider, listing)

    assert result.findings == []
    assert "Scheckheftgepflegt" in result.positive_signals
