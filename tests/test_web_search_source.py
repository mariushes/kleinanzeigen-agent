from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, LlmCall
from app.knowledge.sources.web_search import WebSearchSource
from app.llm.provider import Citation, GroundedResult


class FakeProvider:
    def __init__(self, result: GroundedResult):
        self._result = result
        self.last_call_kwargs = None

    def grounded_completion(self, *, purpose, user, model):
        self.last_call_kwargs = {"purpose": purpose, "user": user, "model": model}
        self._result.purpose = purpose
        self._result.model = model
        return self._result


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_research_returns_document_with_citations_and_logs_call():
    db = make_db()
    provider = FakeProvider(
        GroundedResult(
            text="The CFCA biturbo engine is known for oil consumption...",
            citations=[
                Citation(title="bitdi.eu", url="https://bitdi.eu/cfca"),
                Citation(title="motorio.eu", url="https://motorio.eu/t5"),
            ],
            input_tokens=50,
            output_tokens=200,
        )
    )
    source = WebSearchSource(provider, db)

    documents = source.research("VW T5 2.0 TDI 180 PS common problems")

    assert len(documents) == 1
    doc = documents[0]
    assert doc.source == "web_search"
    assert doc.title == "VW T5 2.0 TDI 180 PS common problems"
    assert "oil consumption" in doc.text
    assert [c.url for c in doc.citations] == ["https://bitdi.eu/cfca", "https://motorio.eu/t5"]
    assert "VW T5 2.0 TDI 180 PS common problems" in provider.last_call_kwargs["user"]
    assert db.query(LlmCall).count() == 1
    assert db.query(LlmCall).one().purpose == "knowledge_research"


def test_research_returns_empty_for_blank_answer():
    db = make_db()
    provider = FakeProvider(GroundedResult(text="   "))
    source = WebSearchSource(provider, db)

    assert source.research("anything") == []
