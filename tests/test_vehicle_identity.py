from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Listing, VehicleIdentity
from app.llm.provider import LLMCallResult
from app.vehicles.identity import (
    ExtractedIdentity,
    build_canonical_label,
    get_or_create_identity,
    identify_listings,
)


class FakeProvider:
    def __init__(self, responses: list[ExtractedIdentity]):
        self._responses = iter(responses)
        self.call_count = 0

    def structured_completion(self, *, purpose, system, user, response_model, model):
        self.call_count += 1
        return LLMCallResult(
            parsed=next(self._responses),
            model=model,
            purpose=purpose,
            input_tokens=10,
            output_tokens=5,
        )


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_listing(db, title="Volkswagen T5 Multivan 2.0 TDI") -> Listing:
    listing = Listing(kleinanzeigen_id=title, url="https://x", title=title, attributes={})
    db.add(listing)
    db.commit()
    return listing


def test_build_canonical_label_joins_present_fields():
    label = build_canonical_label("Volkswagen", "T5 Multivan", "T5.1", "2.0 TDI 180 PS", "Highline")
    assert label == "Volkswagen | T5 Multivan | T5.1 | Highline | 2.0 TDI 180 PS"


def test_build_canonical_label_skips_missing_fields():
    label = build_canonical_label("Volkswagen", "T5 Multivan", None, None, None)
    assert label == "Volkswagen | T5 Multivan"


def test_get_or_create_identity_creates_and_links():
    db = make_db()
    listing = make_listing(db)
    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5 Multivan", engine_code="2.0 TDI 180 PS"),
    ])

    identity = get_or_create_identity(db, provider, listing)

    assert identity.id is not None
    assert listing.identity_id == identity.id
    assert db.query(VehicleIdentity).count() == 1


def test_get_or_create_identity_reuses_existing_by_canonical_label():
    db = make_db()
    listing_a = make_listing(db, title="listing-a")
    listing_b = make_listing(db, title="listing-b")
    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5 Multivan", engine_code="2.0 TDI 180 PS"),
        ExtractedIdentity(brand="volkswagen", model="T5 Multivan", engine_code="2.0 TDI 180 PS"),
    ])

    identity_a = get_or_create_identity(db, provider, listing_a)
    identity_b = get_or_create_identity(db, provider, listing_b)

    assert identity_a.id == identity_b.id
    assert db.query(VehicleIdentity).count() == 1


def test_identify_listings_skips_already_identified():
    db = make_db()
    listing = make_listing(db)
    provider = FakeProvider([
        ExtractedIdentity(brand="Volkswagen", model="T5 Multivan"),
    ])

    identify_listings(db, provider, [listing])
    assert provider.call_count == 1

    # Simulate a rerun: listing now has identity_id set, should not call the LLM again.
    db.refresh(listing)
    identify_listings(db, provider, [listing])
    assert provider.call_count == 1
