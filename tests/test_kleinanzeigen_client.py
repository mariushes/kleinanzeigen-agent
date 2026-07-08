import json
from pathlib import Path

import httpx
import respx

from app.scraping.kleinanzeigen import KleinanzeigenApiError, KleinanzeigenClient

FIXTURES = Path(__file__).parent / "fixtures" / "sidecar"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@respx.mock
def test_search_by_url_caps_to_max_listings():
    respx.post("http://testsidecar/inserate-by-url").mock(
        return_value=httpx.Response(200, json=load_fixture("search_response.json"))
    )
    client = KleinanzeigenClient(base_url="http://testsidecar")

    results = client.search_by_url("https://www.kleinanzeigen.de/s-autos/vw-t5/k0c216", max_listings=2)

    assert len(results) == 2
    assert results[0].adid == "3453063010"
    assert results[0].title.startswith("Volkswagen T5 Multivan")


@respx.mock
def test_search_by_url_computes_max_pages_from_max_listings():
    route = respx.post("http://testsidecar/inserate-by-url").mock(
        return_value=httpx.Response(200, json=load_fixture("search_response.json"))
    )
    client = KleinanzeigenClient(base_url="http://testsidecar")

    client.search_by_url("https://www.kleinanzeigen.de/s-autos/vw-t5/k0c216", max_listings=60)

    assert route.calls.last.request.content
    body = json.loads(route.calls.last.request.content)
    assert body["max_pages"] == 3  # ceil(60 / 25)


@respx.mock
def test_search_by_url_raises_on_sidecar_unreachable():
    respx.post("http://testsidecar/inserate-by-url").mock(side_effect=httpx.ConnectError("refused"))
    client = KleinanzeigenClient(base_url="http://testsidecar")

    try:
        client.search_by_url("https://www.kleinanzeigen.de/s-autos/vw-t5/k0c216", max_listings=10)
        assert False, "expected KleinanzeigenApiError"
    except KleinanzeigenApiError as exc:
        assert "testsidecar" in str(exc)


@respx.mock
def test_get_detail_parses_german_attributes():
    respx.get(url__regex=r"http://testsidecar/inserat/.*").mock(
        return_value=httpx.Response(200, json=load_fixture("detail_response.json"))
    )
    client = KleinanzeigenClient(base_url="http://testsidecar")

    listing = client.get_detail("3453063010-216-8243")

    assert listing.kleinanzeigen_id == "3453063010"
    assert listing.price_eur == 12900
    assert listing.year == 2014
    assert listing.mileage_km == 207149
    assert listing.location == "74196, Neuenstadt"
    assert listing.seller_type == "business"
    assert listing.attributes["fuel"] == "Diesel"
    assert listing.attributes["power_kw"] == round(179 * 0.7355)
    assert listing.attributes["transmission"] == "Automatik"
    assert listing.attributes["condition_label"] == "Unbeschädigtes Fahrzeug"
    assert len(listing.image_urls) == 2


@respx.mock
def test_get_detail_raises_on_deleted_listing():
    respx.get(url__regex=r"http://testsidecar/inserat/.*").mock(
        return_value=httpx.Response(404, json={"detail": {"status": "deleted"}})
    )
    client = KleinanzeigenClient(base_url="http://testsidecar")

    try:
        client.get_detail("0000000000-216-0000")
        assert False, "expected KleinanzeigenApiError"
    except KleinanzeigenApiError as exc:
        assert "deleted" in str(exc) or "expired" in str(exc)
