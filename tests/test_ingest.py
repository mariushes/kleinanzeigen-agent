from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Listing
from app.scraping.ingest import is_likely_wanted_ad, run_search
from app.scraping.kleinanzeigen import ParsedListing, SearchResultItem


class FakeClient:
    def __init__(self, summaries, details_by_segment):
        self._summaries = summaries
        self._details_by_segment = details_by_segment

    def search_by_url(self, search_url, max_listings):
        return self._summaries[:max_listings]

    def get_detail(self, segment):
        return self._details_by_segment[segment]


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_is_likely_wanted_ad_flags_multi_model_ankauf():
    assert is_likely_wanted_ad(
        "Motorschaden Ankauf Golf 6 7 Tiguan Polo T5 T6 Passat Touran GTI",
        "Wir kaufen alle Autos",
    )


def test_is_likely_wanted_ad_allows_genuine_listing():
    assert not is_likely_wanted_ad(
        "Volkswagen T5 Multivan 2.0 TDI LED Navi AHK Lang",
        "Öldruckprobleme! Geringer Kühlwasserverbauch",
    )


def test_is_likely_wanted_ad_allows_private_single_model_wanted_post():
    assert not is_likely_wanted_ad("Suche VW T5 in gutem Zustand", "Zahle fair, bar")


def test_run_search_filters_wanted_ads_and_stores_listings():
    db = make_db()
    summaries = [
        SearchResultItem(
            adid="1", url="https://x/s-anzeige/van-a/1-216-1", title="VW T5 Multivan",
            price="12000", description="Nice van", published_at=None,
        ),
        SearchResultItem(
            adid="2", url="https://x/s-anzeige/ankauf/2-216-1",
            title="Motorschaden Ankauf Golf Tiguan T5 T6 Passat",
            price="", description="Wir kaufen alle Autos", published_at=None,
        ),
    ]
    details = {
        "1-216-1": ParsedListing(
            kleinanzeigen_id="1", url="https://x/1", title="VW T5 Multivan",
            price_eur=12000, year=2015, mileage_km=180000, description_text="Nice van",
            location="Berlin", seller_type="private", image_urls=[], attributes={},
        ),
    }
    client = FakeClient(summaries, details)

    result = run_search(db, "https://x/search", max_listings=10, client=client)

    assert result.fetched_summaries == 2
    assert result.skipped_wanted_ads == 1
    assert result.created == 1
    assert db.query(Listing).count() == 1
    stored = db.query(Listing).first()
    assert stored.kleinanzeigen_id == "1"
    assert result.listing_ids == [stored.id]


def test_run_search_updates_existing_listing_on_rerun():
    db = make_db()
    summaries = [
        SearchResultItem(
            adid="1", url="https://x/s-anzeige/van-a/1-216-1", title="VW T5 Multivan",
            price="12000", description="Nice van", published_at=None,
        ),
    ]
    first_detail = ParsedListing(
        kleinanzeigen_id="1", url="https://x/1", title="VW T5 Multivan",
        price_eur=12000, year=2015, mileage_km=180000, description_text="Nice van",
        location="Berlin", seller_type="private", image_urls=[], attributes={},
    )
    client = FakeClient(summaries, {"1-216-1": first_detail})
    run_search(db, "https://x/search", max_listings=10, client=client)

    updated_detail = first_detail.model_copy(update={"price_eur": 11000})
    client2 = FakeClient(summaries, {"1-216-1": updated_detail})
    result2 = run_search(db, "https://x/search", max_listings=10, client=client2)

    assert result2.created == 0
    assert result2.updated == 1
    assert db.query(Listing).count() == 1
    assert db.query(Listing).first().price_eur == 11000
