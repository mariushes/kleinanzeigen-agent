"""Turns sidecar search results into stored `Listing` rows.

Kleinanzeigen search results mix in "wanted"/buy-my-car-for-cash ads alongside actual
for-sale listings (e.g. "Motorschaden Ankauf Golf, T5, T6..." — a scrap dealer fishing
for leads, not a van for sale). We filter those out heuristically before fetching full
details, since they'd otherwise pollute the comparables pool with junk price signals.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db.models import Listing
from app.scraping.kleinanzeigen import KleinanzeigenClient, SearchResultItem

_WANTED_AD_PATTERN = re.compile(
    r"\b(suche|gesucht|ankauf|kaufe|wir kaufen)\b", re.IGNORECASE
)
_MULTI_MODEL_PATTERN = re.compile(
    r"(t5|t6|golf|passat|touran|tiguan|caddy).*(t5|t6|golf|passat|touran|tiguan|caddy)",
    re.IGNORECASE,
)


def is_likely_wanted_ad(title: str, description: str) -> bool:
    text = f"{title} {description}"
    if not _WANTED_AD_PATTERN.search(title):
        return False
    # A single "Suche VW T5" post from a private buyer is plausible; a dealer ad
    # naming several unrelated models ("Golf 6 7 Tiguan Polo T5 T6 Passat...") is
    # almost always a mass buy-any-car solicitation, not a real listing.
    return bool(_MULTI_MODEL_PATTERN.search(text)) or "ankauf" in title.lower()


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
