from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, KnowledgeEntry, KnowledgeResearchRun, VehicleIdentity
from app.knowledge.builder import (
    RESEARCH_ANGLES,
    build_knowledge_for_identity,
    select_angles,
)
from app.knowledge.extraction import ExtractedEntry, ExtractionResult
from app.knowledge.sources.base import ResearchDocument, SourceCitation


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_identity(db):
    identity = VehicleIdentity(
        brand="Volkswagen", model="T5 Transporter",
        engine_code="2.0 TDI 180 PS (CFCA biturbo)",
        canonical_label="Volkswagen | T5 Transporter | 2.0 TDI 180 PS (CFCA biturbo)",
    )
    db.add(identity)
    db.commit()
    return identity


class FakeSource:
    name = "web_search"

    def __init__(self, docs_per_query):
        self._docs_per_query = docs_per_query
        self.queries = []

    def research(self, query, max_documents):
        self.queries.append(query)
        return self._docs_per_query.get(query, [])[:max_documents]


class FakeProvider:
    """Returns queued extraction results, one per structured_completion call."""

    def __init__(self, extraction_batches):
        self._batches = list(extraction_batches)
        self.calls = 0

    def structured_completion(self, *, purpose, system, user, response_model, model):
        from app.llm.provider import LLMCallResult

        entries = self._batches[self.calls] if self.calls < len(self._batches) else []
        self.calls += 1
        return LLMCallResult(
            parsed=ExtractionResult(entries=entries),
            model=model, purpose=purpose, input_tokens=10, output_tokens=10,
        )


def doc(text, citations=None):
    return ResearchDocument(
        source="web_search", title="research", url=None, text=text,
        citations=[SourceCitation(title=t, url=u) for t, u in (citations or [])],
    )


class AnySource:
    """Returns the same document for any query (angle text varies at runtime)."""

    name = "web_search"

    def __init__(self, doc_batches):
        self._doc_batches = list(doc_batches)
        self.queries = []

    def research(self, query, max_documents):
        self.queries.append(query)
        idx = len(self.queries) - 1
        batch = self._doc_batches[idx] if idx < len(self._doc_batches) else []
        return batch[:max_documents]


def test_queries_include_identity_label_and_bilingual_instruction():
    db = make_db()
    identity = make_identity(db)
    source = AnySource([[doc("x")], [doc("y")]])
    provider = FakeProvider([[], []])

    build_knowledge_for_identity(db, provider, source, identity, max_queries=2)

    assert len(source.queries) == 2
    for q in source.queries:
        assert identity.canonical_label in q
        assert "German" in q  # bilingual sourcing instruction present


def test_build_creates_entries_with_citation_source_url():
    db = make_db()
    identity = make_identity(db)
    source = AnySource([
        [doc("EGR cooler fails.", [("bitdi", "https://bitdi.eu/egr")])],
        [doc("Avoid the biturbo.")],
    ])
    provider = FakeProvider([
        [ExtractedEntry(type="common_problem", component="EGR cooler", detail="Fails via corrosion", supporting_quote="EGR cooler fails")],
        [ExtractedEntry(type="config_advice", component="180 PS biturbo", detail="Avoid, prone to failure")],
    ])

    result = build_knowledge_for_identity(db, provider, source, identity, max_queries=2)

    assert result.queries_run == 2
    assert result.created == 2
    assert result.merged == 0
    egr = next(e for e in db.query(KnowledgeEntry).all() if e.entry_type == "common_problem")
    assert egr.source_url == "https://bitdi.eu/egr"
    assert egr.payload["component"] == "EGR cooler"
    assert egr.payload["source_label"] == "bitdi"


def test_build_merges_duplicate_component_and_bumps_confidence():
    db = make_db()
    identity = make_identity(db)
    source = AnySource([
        [doc("EGR cooler fails.")],
        [doc("The EGR  Cooler is the main weak point.")],  # different casing/spacing
    ])
    provider = FakeProvider([
        [ExtractedEntry(type="common_problem", component="EGR cooler", detail="Corrosion failure")],
        [ExtractedEntry(type="common_problem", component="EGR  Cooler", detail="Main weak point")],
    ])

    result = build_knowledge_for_identity(db, provider, source, identity, max_queries=2)

    assert result.created == 1
    assert result.merged == 1
    entry = db.query(KnowledgeEntry).one()
    assert entry.mention_count == 2
    assert entry.confidence == 0.6  # 0.5 start + 0.1


def test_build_respects_max_queries_cap():
    db = make_db()
    identity = make_identity(db)
    source = AnySource([[doc("x")]])
    provider = FakeProvider([[]])

    result = build_knowledge_for_identity(db, provider, source, identity, max_queries=1)

    assert result.queries_run == 1
    assert len(source.queries) == 1


def test_repeat_runs_advance_to_new_angles():
    db = make_db()
    identity = make_identity(db)
    provider = FakeProvider([[], [], [], []])

    build_knowledge_for_identity(db, provider, AnySource([[doc("a")], [doc("b")]]), identity, max_queries=2)
    first_angles = [r.angle_key for r in db.query(KnowledgeResearchRun).all()]

    build_knowledge_for_identity(db, provider, AnySource([[doc("c")], [doc("d")]]), identity, max_queries=2)
    all_angles = [r.angle_key for r in db.query(KnowledgeResearchRun).all()]
    second_angles = all_angles[len(first_angles):]

    # Second run must not repeat the first run's angles (until the pool is exhausted).
    assert set(first_angles).isdisjoint(second_angles)
    assert len(set(all_angles)) == 4


def test_select_angles_wraps_when_all_covered():
    db = make_db()
    identity = make_identity(db)
    for angle_key, _ in RESEARCH_ANGLES:
        db.add(KnowledgeResearchRun(identity_id=identity.id, angle_key=angle_key))
    db.commit()

    angles = select_angles(db, identity, max_queries=2)

    # Everything covered → reinforce from the top of the pool rather than returning nothing.
    assert angles == RESEARCH_ANGLES[:2]


def test_build_survives_source_failure():
    db = make_db()
    identity = make_identity(db)

    class BrokenSource:
        name = "web_search"
        def research(self, query, max_documents):
            raise RuntimeError("429 quota")

    provider = FakeProvider([])
    result = build_knowledge_for_identity(db, provider, BrokenSource(), identity, max_queries=2)

    assert result.created == 0
    assert len(result.failed) == 2
    assert "429" in result.failed[0]
