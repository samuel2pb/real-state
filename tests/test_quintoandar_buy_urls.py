"""Partner classifieds are ~2/3 of the QA buy result set and live on a
different route. /imovel/<id>/comprar redirects them to /indisponivel and
returns an unhydrated shell, so the URL must be picked per listing kind.
"""

from rental_finder.sources.quintoandar import BASE, QuintoAndarSource


def _src() -> QuintoAndarSource:
    return QuintoAndarSource.__new__(QuintoAndarSource)


CLASSIFIED_HIT = {
    "salePrice": 850000,
    "listingTags": ["CLASSIFIED"],
    "recommendationListingOrigin": "CLASSIFIED",
    "address": {"address": "Alameda dos Anapurus"},
    "neighbourhood": "Moema",
    "type": "Apartamento",
}
OWN_HIT = {
    "salePrice": 900000,
    "listingTags": ["SALE_GOOD_PRICE"],
    "recommendationListingOrigin": "TRANSACTIONAL",
    "address": {"address": "Avenida Ibijaú"},
    "neighbourhood": "Moema",
    "type": "Apartamento",
}


def test_is_classified():
    assert QuintoAndarSource._is_classified(CLASSIFIED_HIT) is True
    assert QuintoAndarSource._is_classified(OWN_HIT) is False
    assert QuintoAndarSource._is_classified({}) is False


def test_is_classified_from_either_marker():
    assert QuintoAndarSource._is_classified({"listingTags": ["CLASSIFIED"]}) is True
    assert (
        QuintoAndarSource._is_classified(
            {"recommendationListingOrigin": "CLASSIFIED"}
        )
        is True
    )


def test_classified_listing_gets_the_classificado_url():
    lst = _src()._parse_buy_search_hit("105576033", CLASSIFIED_HIT, "Moema")
    assert lst.url == f"{BASE}/classificado/105576033/comprar"


def test_own_listing_keeps_the_imovel_url():
    lst = _src()._parse_buy_search_hit("892777121", OWN_HIT, "Moema")
    assert lst.url == f"{BASE}/imovel/892777121/comprar"


def test_detail_fetch_follows_the_listing_url(monkeypatch):
    seen = []
    src = _src()
    src.http = type("H", (), {"get": lambda _s, url, **kw: seen.append(url) or _Resp()})()
    monkeypatch.setattr(QuintoAndarSource, "_extract_next_data", lambda _s, _t: None)
    src._fetch_buy_detail("105576033", f"{BASE}/classificado/105576033/comprar")
    src._fetch_buy_detail("892777121")
    assert seen == [
        f"{BASE}/classificado/105576033/comprar",
        f"{BASE}/imovel/892777121/comprar",
    ]


class _Resp:
    text = ""
