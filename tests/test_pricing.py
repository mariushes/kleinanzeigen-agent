from app.analysis.comparables import Comparable, ComparablesResult
from app.analysis.pricing import format_comparables
from app.db.models import Listing


def _comparable(title, price):
    return Comparable(
        listing=Listing(kleinanzeigen_id=title, url="https://x", title=title, price_eur=price),
        tier="exact_identity",
        delta_description="+5,000 km",
    )


def test_empty_when_no_comparables_and_no_forum_points():
    assert format_comparables(ComparablesResult()) == ""


def test_formats_tiered_comparables_with_deltas():
    comparables = ComparablesResult(
        comparables=[_comparable("Comparable van", 9500)],
        tier_counts={"exact_identity": 1},
    )
    block = format_comparables(comparables)
    assert "Comparable van" in block
    assert "9500 EUR" in block
    assert "exact_identity" in block
    assert "+5,000 km" in block


def test_includes_forum_price_points():
    block = format_comparables(
        ComparablesResult(),
        forum_price_points=[{"price_eur": 9000, "context": "similar engine, 140k km", "source_url": "https://forum/x"}],
    )
    assert "forum" in block.lower()
    assert "9000 EUR" in block
