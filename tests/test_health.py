import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.session import get_db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def db_session():
    # Routes depend on get_db, which otherwise points at the real dev SQLite file —
    # override it here so these HTTP-level tests don't read/write real local data.
    # StaticPool keeps one shared connection alive so the in-memory DB isn't dropped
    # between checkouts (each plain connection would otherwise get its own empty :memory:).
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine)

    def _override():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestSessionLocal()
    app.dependency_overrides.pop(get_db, None)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_renders():
    response = client.get("/")
    assert response.status_code == 200
    assert "Van listings" in response.text


def test_dashboard_shows_empty_state_with_no_listings():
    response = client.get("/")
    assert "No listings analyzed yet" in response.text


def test_create_search_run_redirects_and_schedules_job(monkeypatch):
    scheduled = {}

    def fake_add_task(self, func, *args, **kwargs):
        scheduled["func"] = func
        scheduled["args"] = args

    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", fake_add_task)

    response = client.post(
        "/search-runs",
        data={"search_url": "https://www.kleinanzeigen.de/s-autos/vw-t5/k0c216", "max_listings": 5},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert scheduled["func"].__name__ == "execute_search_run"


def test_search_run_status_returns_fragment_for_unknown_run():
    response = client.get("/search-runs/999999/status")
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_listing_detail_404_for_unknown_listing():
    response = client.get("/listings/999999")
    assert response.status_code == 404
    assert "Listing not found" in response.text


def test_listing_detail_renders_analysis_sections(db_session):
    from app.db.models import Analysis, Listing

    listing = Listing(
        kleinanzeigen_id="d1", url="https://x", title="VW T5 Detail Test",
        price_eur=12000, year=2014, mileage_km=180000,
        description_text="Scheckheftgepflegt.", attributes={},
    )
    db_session.add(listing)
    db_session.commit()
    db_session.add(
        Analysis(
            listing_id=listing.id,
            condition={
                "findings": [
                    {"category": "rust", "severity": "medium", "description": "Rust on sills", "supporting_quote": "Rost"}
                ],
                "positive_signals": ["Scheckheftgepflegt"],
                "summary": "Some rust.",
            },
            price={"rating": "fair", "note": "Priced in line with the closest comparable."},
            reliability={
                "tier": None, "entry_ids": [],
                "deterministic": {
                    "level": "none", "penalty": 0, "bonus": 0,
                    "drivers": [], "positives": [], "has_unrated_entries": False,
                },
            },
            overall_score=62, tier="caution", confidence="low",
            reasoning_text="Overall: some rust, price fair.",
            verdict_axes={
                "overall_score": 62,
                "price": {"rating": "no_data", "note": "No comparables yet.", "has_data": False},
                "condition": {"rating": "fair", "note": "Some rust.", "has_data": True},
                "reliability": {"rating": "no_data", "note": "No KB coverage.", "has_data": False},
                "positives": {"rating": "good", "note": "Scheckheftgepflegt.", "has_data": True},
            },
        )
    )
    db_session.commit()

    response = client.get(f"/listings/{listing.id}")

    assert response.status_code == 200
    assert "VW T5 Detail Test" in response.text
    # Verdict card shows the score and per-axis ratings.
    assert "62/100" in response.text
    assert "Condition red flags" in response.text
    # Axes without evidence render as "No data" rather than a neutral rating.
    assert "No data" in response.text
    assert "Rust on sills" in response.text
    assert "No knowledge base coverage yet" in response.text


def test_listing_detail_renders_unanalyzed_listing(db_session):
    from app.db.models import Listing

    listing = Listing(kleinanzeigen_id="d2", url="https://x", title="Unanalyzed van", attributes={})
    db_session.add(listing)
    db_session.commit()

    response = client.get(f"/listings/{listing.id}")

    assert response.status_code == 200
    assert "has not been analyzed yet" in response.text


def test_knowledge_admin_empty_state(db_session):
    response = client.get("/knowledge")
    assert response.status_code == 200
    assert "No vehicle identities yet" in response.text


def test_knowledge_admin_lists_identity_coverage(db_session):
    from app.db.models import KnowledgeEntry, VehicleIdentity

    identity = VehicleIdentity(
        brand="Volkswagen", model="T5 Transporter",
        canonical_label="Volkswagen | T5 Transporter | 2.0 TDI 140 PS",
    )
    db_session.add(identity)
    db_session.commit()
    db_session.add(
        KnowledgeEntry(
            identity_id=identity.id, entry_type="common_problem",
            payload={"component": "EGR cooler", "detail": "fails"},
            source_url="https://x", mention_count=1, confidence=0.5,
        )
    )
    db_session.commit()

    response = client.get("/knowledge")

    assert response.status_code == 200
    assert "Volkswagen | T5 Transporter | 2.0 TDI 140 PS" in response.text
    assert "Refresh" in response.text  # entry_count > 0 → button says Refresh


def test_collect_knowledge_redirects_and_schedules_job(db_session, monkeypatch):
    from app.db.models import VehicleIdentity

    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5")
    db_session.add(identity)
    db_session.commit()

    scheduled = {}
    monkeypatch.setattr(
        "fastapi.BackgroundTasks.add_task",
        lambda self, func, *a, **k: scheduled.update(func=func, args=a),
    )

    response = client.post(f"/knowledge/{identity.id}/collect", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/knowledge"
    assert scheduled["func"].__name__ == "execute_knowledge_run"
    assert scheduled["args"] == (identity.id,)


def test_listing_detail_shows_live_knowledge_and_reanalyze_prompt(db_session):
    from app.db.models import Analysis, KnowledgeEntry, Listing, VehicleIdentity

    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5")
    db_session.add(identity)
    db_session.commit()
    listing = Listing(
        kleinanzeigen_id="k1", url="https://x", title="VW T5 KB Test",
        attributes={}, identity_id=identity.id,
    )
    db_session.add(listing)
    db_session.commit()
    # Analysis recorded when the KB was empty (no entry_ids used)...
    db_session.add(
        Analysis(
            listing_id=listing.id, condition={"findings": [], "positive_signals": [], "summary": "ok"},
            price={"tier": "fair", "confidence": "low", "fair_price_range": None, "reasoning": "n/a"},
            reliability={"tier": None, "entry_ids": []},
            overall_score=70, tier="buy_candidate", reasoning_text="ok",
        )
    )
    # ...but knowledge has since been collected for this identity.
    db_session.add(
        KnowledgeEntry(
            identity_id=identity.id, entry_type="common_problem",
            payload={"component": "EGR cooler", "detail": "corrodes", "source_label": "t6forum.com"},
            source_url="https://t6forum.com/egr", mention_count=2, confidence=0.6,
        )
    )
    db_session.commit()

    response = client.get(f"/listings/{listing.id}")

    assert response.status_code == 200
    # Live-retrieved KB shows even though the verdict was computed before it existed.
    assert "EGR cooler" in response.text
    assert "t6forum.com" in response.text
    # And the stale-verdict re-analyze prompt appears.
    assert "Re-analyze" in response.text


def test_reanalyze_redirects_and_schedules_job(db_session, monkeypatch):
    from app.db.models import Listing

    listing = Listing(kleinanzeigen_id="r1", url="https://x", title="Reanalyze me", attributes={})
    db_session.add(listing)
    db_session.commit()

    scheduled = {}
    monkeypatch.setattr(
        "fastapi.BackgroundTasks.add_task",
        lambda self, func, *a, **k: scheduled.update(func=func, args=a),
    )

    response = client.post(f"/listings/{listing.id}/reanalyze", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/listings/{listing.id}"
    assert scheduled["func"].__name__ == "execute_reanalyze"
    assert scheduled["args"] == (listing.id,)


def test_knowledge_identity_lists_all_entries(db_session):
    from app.db.models import KnowledgeEntry, VehicleIdentity

    identity = VehicleIdentity(brand="VW", model="T5", canonical_label="VW | T5 | 2.0 TDI")
    db_session.add(identity)
    db_session.commit()
    db_session.add_all([
        KnowledgeEntry(
            identity_id=identity.id, entry_type="common_problem",
            payload={"component": "EGR cooler", "detail": "corrodes", "source_label": "tx-board.de"},
            source_url="https://tx-board.de/x", mention_count=2, confidence=0.6,
        ),
        KnowledgeEntry(
            identity_id=identity.id, entry_type="config_advice",
            payload={"component": "140 PS", "detail": "best balance", "source_label": "motor-talk.de"},
            source_url="https://motor-talk.de/y", mention_count=1, confidence=0.5,
        ),
    ])
    db_session.commit()

    response = client.get(f"/knowledge/{identity.id}")

    assert response.status_code == 200
    assert "EGR cooler" in response.text
    assert "140 PS" in response.text
    assert "tx-board.de" in response.text
    assert "motor-talk.de" in response.text


def test_knowledge_identity_404_for_unknown(db_session):
    response = client.get("/knowledge/999999")
    assert response.status_code == 404
    assert "Vehicle not found" in response.text
