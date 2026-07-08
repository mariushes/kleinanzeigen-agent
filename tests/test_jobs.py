from unittest.mock import patch

from app.db.models import Analysis, SearchRun
from app.jobs import execute_search_run
from app.llm.provider import LLMCallResult
from app.scraping.kleinanzeigen import ParsedListing, SearchResultItem
from app.vehicles.identity import ExtractedIdentity
from app.analysis.condition import ConditionAnalysis


class FakeClient:
    def __init__(self, summaries, details_by_segment):
        self._summaries = summaries
        self._details_by_segment = details_by_segment

    def search_by_url(self, search_url, max_listings):
        return self._summaries[:max_listings]

    def get_detail(self, segment):
        return self._details_by_segment[segment]


class FakeProvider:
    def __init__(self, responses):
        self._responses = iter(responses)

    def structured_completion(self, *, purpose, system, user, response_model, model):
        return LLMCallResult(parsed=next(self._responses), model=model, purpose=purpose, input_tokens=5, output_tokens=2)


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
                ExtractedIdentity(brand="Volkswagen", model="T5 Transporter"),
                clean_condition(),
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
