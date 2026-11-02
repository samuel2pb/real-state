"""`Sqm` means different things per source; the row has to say which."""

from __future__ import annotations

from rental_finder.notion_store import _area_props
from rental_finder.sources.quintoandar import QuintoAndarSource


def test_quintoandar_area_is_unlabelled():
    """Probed live on 125652767: `area` was 141 for a ~119 m2 apartment."""
    lst = QuintoAndarSource()._parse_buy_search_hit(
        "125652767", {"salePrice": 980000, "area": 141}, "Vila Mariana"
    )
    assert lst.sqm == 141.0
    assert lst.sqm_basis == "unspecified"
    assert lst.sqm_total == 0.0


def test_area_props_clear_rather_than_zero():
    lst = QuintoAndarSource()._parse_buy_search_hit(
        "1", {"salePrice": 1, "area": 80}, "Moema"
    )
    props = _area_props(lst)
    assert props["SqmTotal"] == {"number": None}
    assert props["SqmBasis"] == {"select": {"name": "unspecified"}}
