from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.criteria import (
    CriteriaAnalysis,
    CriteriaFinding,
    assess_criteria,
    build_system_prompt,
)
from app.db.models import Base, BuyerCriteriaProfile, Listing, LlmCall
from app.llm.provider import LLMCallResult


class FakeProvider:
    def __init__(self, response: CriteriaAnalysis):
        self._response = response
        self.last_call_kwargs: dict | None = None

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.last_call_kwargs = {"purpose": purpose, "system": system, "user": user, "model": model}
        return LLMCallResult(
            parsed=self._response, model=model, purpose=purpose, input_tokens=30, output_tokens=12,
        )


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_profile(**overrides) -> BuyerCriteriaProfile:
    """A profile with the same *shape* as the seeded ones, but deliberately not the camper
    wording — these tests must pass for any criteria set, not just the shipped one."""
    defaults = dict(
        slug="boat",
        name="Boat towing",
        description="Judge the vehicle as a tow car for a small boat.",
        free_text="I need to tow a 1200 kg boat trailer.",
        flags={"min_tow_capacity_kg": 1200},
        aspects=[
            {"key": "tow_hitch", "label": "Tow hitch", "prompt": "Is a tow hitch fitted?"},
            {"key": "capacity", "label": "Towing capacity", "prompt": "Is capacity sufficient?"},
        ],
    )
    return BuyerCriteriaProfile(**{**defaults, **overrides})


def make_listing(**overrides) -> Listing:
    defaults = dict(
        kleinanzeigen_id="1",
        url="https://x",
        title="VW T5 Transporter",
        description_text="Anhängerkupplung verbaut, 2.5 TDI.",
        attributes={},
    )
    return Listing(**{**defaults, **overrides})


def test_system_prompt_is_assembled_from_the_profile_row():
    prompt = build_system_prompt(make_profile())

    # Everything the model is asked to judge comes from the row, not from code.
    assert "Boat towing" in prompt
    assert "I need to tow a 1200 kg boat trailer." in prompt
    assert "min_tow_capacity_kg=1200" in prompt
    assert "tow_hitch (Tow hitch): Is a tow hitch fitted?" in prompt
    assert "capacity (Towing capacity): Is capacity sufficient?" in prompt


def test_system_prompt_scopes_positive_signals_to_the_buyers_purpose():
    """Positive signals must be criteria-specific, not the ad's general merits.

    Observed live: a camper assessment listed "New TÜV valid until 2028" and "recent oil
    change / new tyres" as camper positives — real strengths, but they belong to the
    condition axis and were being double-counted. Same anti-double-counting rule that
    keeps model-general reliability out of `condition.py`.
    """
    prompt = build_system_prompt(make_profile())

    assert "SPECIFIC TO THE BUYER'S PURPOSE" in prompt
    # The concrete leak categories are named, not just gestured at.
    for banned in ("TÜV", "service history", "oil change", "owners"):
        assert banned in prompt
    # An empty list must read as a valid outcome, or the model will pad it.
    assert "empty list" in prompt


def test_system_prompt_tolerates_a_minimal_profile():
    prompt = build_system_prompt(
        make_profile(description=None, free_text=None, flags={})
    )

    assert "Boat towing" in prompt
    assert "tow_hitch" in prompt


def test_assess_criteria_returns_findings_and_logs_the_call():
    db = make_db()
    listing = make_listing()
    profile = make_profile()
    db.add_all([listing, profile])
    db.commit()

    expected = CriteriaAnalysis(
        findings=[
            CriteriaFinding(
                aspect="tow_hitch",
                verdict="meets",
                description="Tow hitch already fitted.",
                supporting_quote="Anhängerkupplung verbaut",
            ),
            CriteriaFinding(
                aspect="capacity",
                verdict="unknown",
                description="The ad does not state the towing capacity.",
            ),
        ],
        positive_signals=["Tow hitch already fitted"],
        summary="Usable as a tow car; capacity needs checking with the seller.",
    )
    provider = FakeProvider(expected)

    result = assess_criteria(db, provider, listing, profile)

    assert result == expected
    assert provider.last_call_kwargs["purpose"] == "criteria_analysis"
    assert "Anhängerkupplung verbaut" in provider.last_call_kwargs["user"]
    assert "Boat towing" in provider.last_call_kwargs["system"]

    call = db.query(LlmCall).one()
    assert call.related_entity == f"listing:{listing.id}"
    assert call.purpose == "criteria_analysis"


def test_has_data_is_false_when_the_ad_is_silent_on_every_aspect():
    """An ad that says nothing about the requirements must read as "no data", not as a
    bad fit — this is what drives the grey `no_data` axis in the verdict."""
    silent = CriteriaAnalysis(
        findings=[
            CriteriaFinding(aspect="tow_hitch", verdict="unknown", description="Not stated."),
            CriteriaFinding(aspect="capacity", verdict="unknown", description="Not stated."),
        ],
        positive_signals=[],
        summary="The ad says nothing about towing.",
    )

    assert silent.has_data is False


def test_has_data_is_false_when_there_are_no_findings_at_all():
    assert CriteriaAnalysis(findings=[], positive_signals=[], summary="").has_data is False


def test_has_data_is_true_when_any_aspect_was_actually_judged():
    partial = CriteriaAnalysis(
        findings=[
            CriteriaFinding(aspect="tow_hitch", verdict="meets", description="Fitted."),
            CriteriaFinding(aspect="capacity", verdict="unknown", description="Not stated."),
        ],
        positive_signals=[],
        summary="Hitch fitted, capacity unclear.",
    )

    assert partial.has_data is True
