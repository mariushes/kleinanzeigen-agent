import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.comparables import find_comparables
from app.db.models import Base, Listing, VehicleIdentity

_id_counter = itertools.count(1)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_identity(db, **kwargs) -> VehicleIdentity:
    defaults = dict(brand="Volkswagen", model="T5 Multivan", canonical_label=None)
    defaults.update(kwargs)
    if defaults["canonical_label"] is None:
        defaults["canonical_label"] = " | ".join(
            str(v) for v in (defaults["brand"], defaults["model"], defaults.get("generation"), defaults.get("engine_code"))
            if v
        )
    identity = VehicleIdentity(**defaults)
    db.add(identity)
    db.commit()
    return identity


def make_listing(db, identity=None, price_eur=10000, mileage_km=100000, year=2015, power_kw=100, **kwargs) -> Listing:
    listing = Listing(
        kleinanzeigen_id=kwargs.pop("kleinanzeigen_id", f"id-{next(_id_counter)}"),
        url="https://x",
        title=kwargs.pop("title", "listing"),
        price_eur=price_eur,
        mileage_km=mileage_km,
        year=year,
        attributes={"power_kw": power_kw},
        identity_id=identity.id if identity else None,
        **kwargs,
    )
    db.add(listing)
    db.commit()
    return listing


def test_no_identity_returns_empty_result():
    db = make_db()
    target = make_listing(db, identity=None)

    result = find_comparables(db, target)

    assert result.comparables == []
    assert result.decent_count == 0


def test_exact_identity_tier_preferred_and_sorted_by_distance():
    db = make_db()
    identity = make_identity(db, generation="T5.1", engine_code="2.0 TDI 180 PS")
    target = make_listing(db, identity=identity, mileage_km=100000, year=2015, power_kw=132)
    close = make_listing(db, identity=identity, mileage_km=105000, year=2015, power_kw=132)
    far = make_listing(db, identity=identity, mileage_km=250000, year=2010, power_kw=132)

    result = find_comparables(db, target, target_count=8)

    assert result.tier_counts == {"exact_identity": 2}
    assert result.decent_count == 2
    assert [c.listing.id for c in result.comparables] == [close.id, far.id]
    assert "km" in result.comparables[0].delta_description


def test_falls_back_to_same_generation_when_no_exact_match():
    db = make_db()
    target_identity = make_identity(db, generation="T5.1", engine_code="2.0 TDI 180 PS")
    sibling_identity = make_identity(db, generation="T5.1", engine_code="2.0 TDI 179 PS")
    target = make_listing(db, identity=target_identity)
    sibling = make_listing(db, identity=sibling_identity)

    result = find_comparables(db, target, target_count=8)

    assert result.tier_counts == {"same_generation": 1}
    assert result.comparables[0].listing.id == sibling.id
    assert result.decent_count == 1


def test_falls_back_to_same_model_when_no_generation_match():
    db = make_db()
    target_identity = make_identity(db, generation="T5.1")
    other_gen_identity = make_identity(db, generation="T5.2")
    target = make_listing(db, identity=target_identity)
    other_gen = make_listing(db, identity=other_gen_identity)

    result = find_comparables(db, target, target_count=8)

    assert result.tier_counts == {"same_model": 1}
    assert result.decent_count == 0  # same_model alone isn't "decent"
    assert result.comparables[0].listing.id == other_gen.id


def test_excludes_listings_without_price():
    db = make_db()
    identity = make_identity(db)
    target = make_listing(db, identity=identity)
    make_listing(db, identity=identity, price_eur=None)

    result = find_comparables(db, target, target_count=8)

    assert result.comparables == []


def test_respects_target_count_cap():
    db = make_db()
    identity = make_identity(db)
    target = make_listing(db, identity=identity)
    for i in range(5):
        make_listing(db, identity=identity, mileage_km=100000 + i * 1000)

    result = find_comparables(db, target, target_count=3)

    assert len(result.comparables) == 3
    assert result.tier_counts == {"exact_identity": 3}
