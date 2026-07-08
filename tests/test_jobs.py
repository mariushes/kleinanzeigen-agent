from unittest.mock import patch

from app.db.models import Analysis, SearchRun
from app.jobs import execute_search_run
from app.llm.provider import LLMCallResult
from app.scraping.kleinanzeigen import ParsedListing, SearchResultItem
from app.vehicles.identity import ExtractedIdentity
from app.analysis.condition import ConditionAnalysis
from app.analysis.judgment import AxisRating, Judgment


class FakeClient:
    def __init__(self, summaries, details_by_segment):
        self._summaries = summaries
        self._details_by_segment = details_by_segment

    def search_by_url(self, search_url, max_listings):
        return self._summaries[:max_listings]

    def get_detail(self, segment):
        return self._details_by_segment[segment]


class FakeProvider:
    """Serves queued responses by matching type, so the per-listing call sequence
    (identity → condition → judgment, interleaved with knowledge extraction) each gets the
    right shape without depending on exact ordering across listings."""

    def __init__(self, responses):
        self._responses = list(responses)

    def structured_completion(self, *, purpose, system, user, response_model, model):
        for i, r in enumerate(self._responses):
            if isinstance(r, response_model):
                parsed = self._responses.pop(i)
                return LLMCallResult(parsed=parsed, model=model, purpose=purpose, input_tokens=5, output_tokens=2)
        raise AssertionError(f"no queued response of type {response_model.__name__}")


def make_summary(adid, title):
    return SearchResultItem(adid=adid, url=f"https://x/s-anzeige/van/{adid}-216-1", price="9000", title=title, description="ok")


def make_detail(adid, title):
    return ParsedListing(
        kleinanzeigen_id=adid, url=f"https://x/{adid}", title=title, price_eur=9000,
        year=2015, mileage_km=150000, description_text="clean van", location="Berlin",
        seller_type="private", image_urls=[], attributes={},
    )


def clean_condition():
    return ConditionAnalysis(findings=[], positive_signals=[], summary="Looks fine.")


def a_judgment():
    return Judgment(
        overall_score=70,
        recommendation="buy_candidate",
        price=AxisRating(rating="fair", note="in line"),
        condition=AxisRating(rating="good", note="clean"),
        reliability=AxisRating(rating="good", note="dependable"),
        positives=AxisRating(rating="none", note="nothing notable"),
        reasoning="Fine.",
    )


def test_execute_search_run_happy_path(tmp_path):
    db_path = tmp_path / "test.db"

    from app.db.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    with patch("app.jobs.SessionLocal", TestSession):
        db = TestSession()
        search_run = SearchRun(search_url="https://x/search", max_listings=2, status="pending")
        db.add(search_run)
        db.commit()
        run_id = search_run.id
        db.close()

        client = FakeClient(
            summaries=[make_summary("1", "VW T5 Multivan"), make_summary("2", "VW T5 Transporter")],
            details_by_segment={
                "1-216-1": make_detail("1", "VW T5 Multivan"),
                "2-216-1": make_detail("2", "VW T5 Transporter"),
            },
        )
        provider = FakeProvider(
            [
                ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
                clean_condition(),
                a_judgment(),
                ExtractedIdentity(brand="Volkswagen", model="T5 Transporter"),
                clean_condition(),
                a_judgment(),
            ]
        )

        execute_search_run(run_id, client=client, provider=provider)

        db = TestSession()
        run = db.get(SearchRun, run_id)
        assert run.status == "done"
        assert run.counts["scraped"] == 2
        assert run.counts["analyzed"] == 2
        assert db.query(Analysis).count() == 2


def test_execute_search_run_marks_error_on_failure(tmp_path):
    db_path = tmp_path / "test2.db"
    from app.db.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    with patch("app.jobs.SessionLocal", TestSession):
        db = TestSession()
        search_run = SearchRun(search_url="https://x/search", max_listings=2, status="pending")
        db.add(search_run)
        db.commit()
        run_id = search_run.id
        db.close()

        class ExplodingClient:
            def search_by_url(self, *a, **kw):
                raise RuntimeError("sidecar unreachable")

        execute_search_run(run_id, client=ExplodingClient(), provider=FakeProvider([]))

        db = TestSession()
        run = db.get(SearchRun, run_id)
        assert run.status == "error"
        assert "sidecar unreachable" in run.error


def test_execute_search_run_auto_collects_for_new_identity(tmp_path, monkeypatch):
    db_path = tmp_path / "test3.db"
    from app.db.models import Base, KnowledgeEntry, KnowledgeResearchRun
    from app.knowledge.extraction import ExtractedEntry, ExtractionResult
    from app.llm.provider import Citation, GroundedResult
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    # Cap auto-collect to 1 query to keep the fake-response ordering simple.
    from app.config import get_settings
    settings = get_settings().model_copy(update={"auto_collect_max_queries": 1})
    monkeypatch.setattr("app.jobs.get_settings", lambda: settings)

    class GroundingFakeProvider(FakeProvider):
        def grounded_completion(self, *, purpose, user, model):
            return GroundedResult(
                text="The engine is considered robust; DSG can fail early.",
                citations=[Citation(title="t6forum.com", url="https://t6forum.com/x")],
                model=model, purpose=purpose, input_tokens=5, output_tokens=5,
            )

    with patch("app.jobs.SessionLocal", TestSession):
        db = TestSession()
        search_run = SearchRun(search_url="https://x/search", max_listings=2, status="pending")
        db.add(search_run)
        db.commit()
        run_id = search_run.id
        db.close()

        client = FakeClient(
            summaries=[make_summary("1", "VW T5 Multivan"), make_summary("2", "VW T5 Multivan lang")],
            details_by_segment={
                "1-216-1": make_detail("1", "VW T5 Multivan"),
                "2-216-1": make_detail("2", "VW T5 Multivan lang"),
            },
        )
        # Both listings resolve to the SAME identity → only one auto-collect. The provider
        # serves responses by type, so we just supply: two identities, one knowledge
        # extraction, and a condition + holistic judgment per listing.
        provider = GroundingFakeProvider(
            [
                ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
                ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
                ExtractionResult(entries=[
                    ExtractedEntry(type="strength", component="engine", detail="considered robust"),
                    ExtractedEntry(type="common_problem", component="DSG", detail="fails early", severity="major"),
                ]),
                clean_condition(),
                clean_condition(),
                a_judgment(),
                a_judgment(),
            ]
        )

        execute_search_run(run_id, client=client, provider=provider)

        db = TestSession()
        run = db.get(SearchRun, run_id)
        assert run.status == "done"
        assert run.counts["knowledge_collected"] == 1  # same identity → collected once
        assert db.query(KnowledgeEntry).count() == 2
        assert db.query(KnowledgeResearchRun).count() == 1
        # First listing's verdict already used the freshly collected knowledge.
        first_analysis = db.query(Analysis).order_by(Analysis.id).first()
        assert first_analysis.reliability["entry_ids"]
        assert first_analysis.reliability["deterministic"]["bonus"] > 0


def test_auto_collect_skips_identity_with_existing_entries(tmp_path):
    db_path = tmp_path / "test4.db"
    from app.db.models import Base, KnowledgeEntry, VehicleIdentity
    from app.jobs import _maybe_auto_collect
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5")
    db.add(identity)
    db.commit()
    db.add(KnowledgeEntry(
        identity_id=identity.id, entry_type="common_problem",
        payload={"component": "x", "detail": "y"}, source_url="https://x",
    ))
    db.commit()

    class MustNotBeCalledProvider:
        def grounded_completion(self, **kw):
            raise AssertionError("should not collect for an identity with entries")

    attempted = _maybe_auto_collect(db, MustNotBeCalledProvider(), identity, set())
    assert attempted is False
