"""Client for the vendored ebay-kleinanzeigen-api sidecar (vendor/ebay-kleinanzeigen-api).

We don't scrape kleinanzeigen.de ourselves: that sidecar already handles the fragile
parts (Playwright, anti-bot pacing, deleted-ad detection). This module is a thin,
typed HTTP client on top of it plus parsing of its German-language `details` dict into
the fields our `Listing` model needs.
"""

import math
import re
import uuid
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import get_settings

RESULTS_PER_PAGE = 25


class SearchResultItem(BaseModel):
    adid: str
    url: str
    title: str
    price: str
    description: str
    published_at: str | None = None


class ParsedListing(BaseModel):
    kleinanzeigen_id: str
    url: str
    title: str
    price_eur: int | None
    year: int | None
    mileage_km: int | None
    description_text: str | None
    location: str | None
    seller_type: str | None
    image_urls: list[str]
    attributes: dict[str, Any]


class KleinanzeigenApiError(RuntimeError):
    """Raised when the sidecar is unreachable or returns an unexpected response."""


class KleinanzeigenClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.kleinanzeigen_api_base_url).rstrip("/")
        self.timeout = timeout or settings.kleinanzeigen_api_timeout_seconds

    def search_by_url(self, search_url: str, max_listings: int) -> list[SearchResultItem]:
        max_pages = max(1, math.ceil(max_listings / RESULTS_PER_PAGE))
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/inserate-by-url",
                    json={"url": search_url, "max_pages": max_pages},
                )
        except httpx.ConnectError as exc:
            raise KleinanzeigenApiError(
                f"Could not reach the kleinanzeigen-api sidecar at {self.base_url}. "
                "Is it running? See CLAUDE.md for how to start it."
            ) from exc

        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise KleinanzeigenApiError(f"Sidecar search failed: {payload}")

        items = [SearchResultItem.model_validate(item) for item in payload["results"]]
        return items[:max_listings]

    def get_detail(self, adid_or_segment: str) -> ParsedListing:
        batch_id = uuid.uuid4().hex
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    f"{self.base_url}/inserat/{adid_or_segment}",
                    params={"batch_id": batch_id},
                )
        except httpx.ConnectError as exc:
            raise KleinanzeigenApiError(
                f"Could not reach the kleinanzeigen-api sidecar at {self.base_url}. "
                "Is it running? See CLAUDE.md for how to start it."
            ) from exc

        if response.status_code == 404:
            raise KleinanzeigenApiError(f"Listing {adid_or_segment} is deleted or expired")
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise KleinanzeigenApiError(f"Sidecar detail fetch failed: {payload}")

        return parse_listing_detail(payload["data"])


def _parse_mileage(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def _parse_first_registration_year(value: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", value)
    return int(match.group()) if match else None


def _parse_power_kw(value: str) -> int | None:
    match = re.search(r"(\d+)\s*PS", value, re.IGNORECASE)
    if match:
        return round(int(match.group(1)) * 0.7355)
    match = re.search(r"(\d+)\s*kW", value, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_listing_detail(data: dict[str, Any]) -> ParsedListing:
    details: dict[str, str] = data.get("details", {})
    price = data.get("price") or {}
    location = data.get("location") or {}
    seller = data.get("seller") or {}
    media = data.get("media") or {}
    image_urls = media.get("images", {}).get("urls", [])

    mileage_km = _parse_mileage(details["Kilometerstand"]) if "Kilometerstand" in details else None
    year = (
        _parse_first_registration_year(details["Erstzulassung"])
        if "Erstzulassung" in details
        else None
    )
    power_kw = _parse_power_kw(details["Leistung"]) if "Leistung" in details else None

    attributes = {
        "raw_details": details,
        "features": data.get("features", []),
        "fuel": details.get("Kraftstoffart"),
        "power_kw": power_kw,
        "transmission": details.get("Getriebe"),
        "vehicle_type": details.get("Fahrzeugtyp"),
        "condition_label": details.get("Fahrzeugzustand"),
        "brand_raw": details.get("Marke"),
        "model_raw": details.get("Modell"),
        "categories": data.get("categories", []),
        "status": data.get("status"),
    }

    location_str = ", ".join(filter(None, [location.get("zip"), location.get("city")])) or None

    price_amount = price.get("amount")
    price_eur = int(float(price_amount)) if price_amount not in (None, "") else None

    return ParsedListing(
        kleinanzeigen_id=data["id"],
        url=data.get("url_redirected") or data.get("url_requested", ""),
        title=data["title"],
        price_eur=price_eur,
        year=year,
        mileage_km=mileage_km,
        description_text=data.get("description"),
        location=location_str,
        seller_type=seller.get("type"),
        image_urls=image_urls,
        attributes=attributes,
    )
