from rental_finder.models import Listing
from rental_finder.pipeline import _matches_filters


def _mk(**over):
    base = dict(
        source="zap",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        price_rent=5000,
        price_total=5500,
        bedrooms=2,
        bathrooms=2,
        suites=1,
        parking=1,
        sqm=80,
        pets=True,
        furnished=False,
        lat=None,
        lng=None,
    )
    base.update(over)
    return Listing(**base)


def test_match_ok():
    assert _matches_filters(_mk())


def test_price_out():
    assert not _matches_filters(_mk(price_total=9999))


def test_sqm_too_small():
    assert not _matches_filters(_mk(sqm=40))


def test_no_pets():
    assert not _matches_filters(_mk(pets=False))
