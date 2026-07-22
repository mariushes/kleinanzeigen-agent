"""Turns sidecar search results into stored `Listing` rows.

Kleinanzeigen search results mix in "wanted"/buy-my-car-for-cash ads alongside actual
for-sale listings (e.g. "Motorschaden Ankauf Golf, T5, T6..." — a scrap dealer fishing
for leads — or "SUCHE ausgebauten Camper Van" from a private buyer). We filter those out
before fetching full details: they have no vehicle to analyze, so they'd otherwise spend
LLM calls on nothing and pollute the comparables pool with junk price signals.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db.models import Listing
from app.scraping.kleinanzeigen import KleinanzeigenClient, SearchResultItem

_WANTED_AD_PATTERN = re.compile(
    r"\b(suche|gesucht|ankauf|ankaufe|kaufe|wir kaufen)\b", re.IGNORECASE
)


def is_likely_wanted_ad(title: str, description: str = "") -> bool:  # noqa: ARG001
    """True for ads where someone wants to *buy* a vehicle rather than sell one.

    Any wanted post is skipped, whether it's a dealer fishing for leads or a private
    "Suche VW T5" — neither has a vehicle to analyze, so letting one through spends LLM
    calls on nothing and pollutes the comparables pool with a wished-for price.

    Matching is title-only and deliberately brand-agnostic: an earlier version required a
    second signal (two model names from a hardcoded VW list) before skipping, which let
    "SUCHE Ausgebauten Camper Van: T5, Vito, Transit, Trafic" through — only `T5` was on
    the list. Don't reintroduce a model-name list here. `description` is unused today but
    kept in the signature for a future body-text refinement.
    """
    return bool(_WANTED_AD_PATTERN.search(title))


@dataclass
class IngestResult:
    search_url: str
    requested: int
    fetched_summaries: int = 0
    skipped_wanted_ads: int = 0
    created: int = 0
    updated: int = 0
    failed: list[str] = field(default_factory=list)
    listing_ids: list[int] = field(default_factory=list)

    @property
    def total_stored(self) -> int:
        return self.created + self.updated


def run_search(
    db: Session,
    search_url: str,
    max_listings: int,
    client: KleinanzeigenClient | None = None,
) -> IngestResult:
    client = client or KleinanzeigenClient()
    result = IngestResult(search_url=search_url, requested=max_listings)

    summaries: list[SearchResultItem] = client.search_by_url(search_url, max_listings)
    result.fetched_summaries = len(summaries)

    for summary in summaries:
        if is_likely_wanted_ad(summary.title, summary.description):
            result.skipped_wanted_ads += 1
            continue

        segment = summary.url.rsplit("/", 1)[-1]
        try:
            parsed = client.get_detail(segment)
        except Exception as exc:  # noqa: BLE001 - one bad listing shouldn't abort the batch
            result.failed.append(f"{summary.adid}: {exc}")
            continue

        existing = (
            db.query(Listing).filter(Listing.kleinanzeigen_id == parsed.kleinanzeigen_id).first()
        )
        if existing:
            existing.title = parsed.title
            existing.price_eur = parsed.price_eur
            existing.year = parsed.year
            existing.mileage_km = parsed.mileage_km
            existing.attributes = parsed.attributes
            existing.description_text = parsed.description_text
            existing.location = parsed.location
            existing.seller_type = parsed.seller_type
            existing.image_urls = parsed.image_urls
            existing.status = "active"
            result.updated += 1
            touched_listing = existing
        else:
            touched_listing = Listing(
                kleinanzeigen_id=parsed.kleinanzeigen_id,
                url=parsed.url,
                title=parsed.title,
                price_eur=parsed.price_eur,
                year=parsed.year,
                mileage_km=parsed.mileage_km,
                attributes=parsed.attributes,
                description_text=parsed.description_text,
                location=parsed.location,
                seller_type=parsed.seller_type,
                image_urls=parsed.image_urls,
            )
            db.add(touched_listing)
            result.created += 1

        db.flush()
        result.listing_ids.append(touched_listing.id)

    db.commit()
    return result
