"""Tests for the read-side service functions backing the web routes and chat tools.

Services take a Session and return plain data, so these run against a fresh in-memory DB
with no patching. The agent-tool wrappers are covered separately in test_agent_tools.py.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Analysis, Base, KnowledgeEntry, Listing, VehicleIdentity
from app.services import knowledge as knowledge_service
from app.services import listings as listings_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def make_identity(db, label="Volkswagen | T5") -> VehicleIdentity:
    identity = VehicleIdentity(brand="Volkswagen", model="T5", canonical_label=label)
    db.add(identity)
    db.commit()
    return identity


def make_listing(db, *, title="VW T5", identity_id=None, **kwargs) -> Listing:
    listing = Listing(
        kleinanzeigen_id=kwargs.pop("kleinanzeigen_id", title),
        url=kwargs.pop("url", "https://example.com/x"),
        title=title,
        identity_id=identity_id,
        **kwargs,
    )
    db.add(listing)
    db.commit()
    return listing


def make_analysis(db, listing_id, **kwargs) -> Analysis:
    analysis = Analysis(listing_id=listing_id, overall_score=kwargs.pop("overall_score", 70), **kwargs)
    db.add(analysis)
    db.commit()
    return analysis


def make_entry(db, identity_id, *, entry_type="known_issue", confidence=0.5, **kwargs) -> KnowledgeEntry:
    entry = KnowledgeEntry(
        identity_id=identity_id,
        entry_type=entry_type,
        payload=kwargs.pop("payload", {"summary": "x"}),
        source_url=kwargs.pop("source_url", "https://forum.example/thread"),
        confidence=confidence,
        **kwargs,
    )
    db.add(entry)
    db.commit()
    return entry


# --- listings service ---------------------------------------------------------


def test_list_listings_returns_all(db):
    make_listing(db, title="a", kleinanzeigen_id="a")
    make_listing(db, title="b", kleinanzeigen_id="b")

    assert {l.title for l in listings_service.list_listings(db)} == {"a", "b"}


def test_get_listing_found_and_missing(db):
    listing = make_listing(db)

    assert listings_service.get_listing(db, listing.id).id == listing.id
    assert listings_service.get_listing(db, 999) is None


def test_latest_analysis_picks_most_recent_by_created_at(db):
    listing = make_listing(db)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    make_analysis(db, listing.id, overall_score=40, created_at=base)
    newest = make_analysis(db, listing.id, overall_score=88, created_at=base + timedelta(hours=1))

    # Reload uniformly: SQLite reads DateTime back as naive, so mixing in-session (aware)
    # and reloaded (naive) rows would break the comparison. Production (Postgres) is aware
    # throughout; this just keeps the test's rows on one side of that line.
    db.expire_all()

    assert listings_service.latest_analysis(listing).id == newest.id


def test_latest_analysis_none_without_analyses(db):
    listing = make_listing(db)
    assert listings_service.latest_analysis(listing) is None


# --- knowledge service --------------------------------------------------------


def test_entries_for_listing_returns_identity_entries_ordered(db):
    identity = make_identity(db)
    listing = make_listing(db, identity_id=identity.id)
    make_entry(db, identity.id, confidence=0.3)
    make_entry(db, identity.id, confidence=0.9)

    entries = knowledge_service.entries_for_listing(db, listing.id)

    # list_identity_entries orders by entry_type then confidence desc
    assert [e.confidence for e in entries] == [0.9, 0.3]


def test_entries_for_listing_only_that_identity(db):
    identity_a = make_identity(db, label="VW | T5")
    identity_b = make_identity(db, label="VW | T6")
    listing = make_listing(db, identity_id=identity_a.id)
    make_entry(db, identity_a.id, payload={"summary": "a"})
    make_entry(db, identity_b.id, payload={"summary": "b"})

    entries = knowledge_service.entries_for_listing(db, listing.id)

    assert [e.payload for e in entries] == [{"summary": "a"}]


def test_entries_for_listing_no_identity(db):
    listing = make_listing(db, identity_id=None)
    assert knowledge_service.entries_for_listing(db, listing.id) == []


def test_entries_for_listing_unknown_listing(db):
    assert knowledge_service.entries_for_listing(db, 999) == []
