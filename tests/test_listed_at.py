from datetime import date

from rental_finder.models import Listing, parse_source_date
from rental_finder.notion_store import NotionStore, _listed_at_props
from rental_finder.sources.quintoandar import QuintoAndarSource
from rental_finder.sources.zapimoveis import ZapSource


def test_parse_source_date_formats():
    assert parse_source_date("2025-08-28T18:56:59.671Z") == date(2025, 8, 28)
    assert parse_source_date("2025-08-28T18:56:59+00:00") == date(2025, 8, 28)
    assert parse_source_date("2025-08-28") == date(2025, 8, 28)
    assert parse_source_date(1756405019671) == date(2025, 8, 28)  # epoch millis
    assert parse_source_date(date(2025, 8, 28)) == date(2025, 8, 28)


def test_parse_source_date_rejects_garbage():
    for bad in (None, "", 0, "n/a", "0000", {}, []):
        assert parse_source_date(bad) is None


def _zap_hit(price_key: str) -> dict:
    return {
        "listing": {
            "id": "123",
            "createdAt": "2025-04-11T18:48:43.502Z",
            "pricingInfos": [
                {
                    "businessType": "RENTAL" if price_key == "rent" else "SALE",
                    "price": 5000,
                    "rentalInfo": {"monthlyRentalTotalPrice": 6000},
                }
            ],
            "address": {"neighborhood": "Moema"},
            "bedrooms": [2],
            "bathrooms": [2],
            "suites": [1],
            "parkingSpaces": [1],
            "usableAreas": [80],
            "unitTypes": ["APARTMENT"],
        },
        "link": {"href": "/imovel/123/"},
    }


def test_zap_rent_captures_created_at():
    lst = ZapSource._parse_one(ZapSource.__new__(ZapSource), _zap_hit("rent"), "Moema")
    assert lst.listed_at == date(2025, 4, 11)


def test_zap_buy_captures_created_at():
    lst = ZapSource._parse_buy_one(
        ZapSource.__new__(ZapSource), _zap_hit("sale"), "Moema"
    )
    assert lst.listed_at == date(2025, 4, 11)


def test_zap_missing_created_at_is_none():
    hit = _zap_hit("rent")
    del hit["listing"]["createdAt"]
    lst = ZapSource._parse_one(ZapSource.__new__(ZapSource), hit, "Moema")
    assert lst.listed_at is None


def _mk(**kw) -> Listing:
    base = dict(
        source="zap",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        price_rent=5000,
        price_total=6000,
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
    base.update(kw)
    return Listing(**base)


def test_listed_at_props_written_only_when_known():
    assert _listed_at_props(_mk()) == {}
    assert _listed_at_props(_mk(listed_at=date(2025, 4, 11))) == {
        "ListedAt": {"date": {"start": "2025-04-11"}}
    }


def test_properties_carry_listed_at_in_both_modes():
    store = NotionStore.__new__(NotionStore)
    for mode in ("rent", "buy"):
        props = store._properties(_mk(mode=mode, listed_at=date(2025, 4, 11)))
        assert props["ListedAt"] == {"date": {"start": "2025-04-11"}}
        assert "ListedAt" not in store._properties(_mk(mode=mode))


def test_availability_formula_counts_from_listed_at():
    expr = NotionStore.AVAILABILITY_DAYS_FORMULA
    assert 'prop("ListedAt")' in expr
    assert 'prop("FirstSeen")' in expr  # fallback for sources without a date
    assert 'dateBetween(finish, start, "days")' in expr


# --- QuintoAndar: dates live in the detail payload we already fetch ----------

def _qa() -> QuintoAndarSource:
    return QuintoAndarSource.__new__(QuintoAndarSource)


def _house_info(**kw) -> dict:
    hi = {
        "lastPublishedDate": "2026-07-10T17:47:52.000Z",
        "listings": [
            {
                "businessContext": "SALE",
                "status": "despublicado",
                "firstPublicationDate": "2025-04-11T15:35:42.000+0000",
                "lastPublicationDate": "2025-04-11T15:35:42.000+0000",
            },
            {
                "businessContext": "RENT",
                "status": "publicado",
                "firstPublicationDate": "2016-01-29T22:43:47.000+0000",
                "lastPublicationDate": "2026-07-10T17:47:52.000+0000",
            },
        ],
    }
    hi.update(kw)
    return hi


def test_qa_rent_uses_last_publication_of_the_published_rent_listing():
    lst = _mk()
    _qa()._apply_listed_at(lst, _house_info())
    # relisted in 2026, not the 2016 first publication, and not the SALE entry
    assert lst.listed_at == date(2026, 7, 10)


def test_qa_buy_reads_the_sale_context():
    hi = _house_info(
        listings=[
            {
                "businessContext": "SALE",
                "status": "publicado",
                "firstPublicationDate": "2026-08-28T17:17:42.000+0000",
                "lastPublicationDate": "2026-08-28T17:17:42.000+0000",
            },
            {"businessContext": "RENT", "status": "unpublished"},
        ]
    )
    lst = _mk(mode="buy")
    _qa()._apply_listed_at(lst, hi)
    assert lst.listed_at == date(2026, 8, 28)


def test_qa_falls_back_to_last_published_date():
    lst = _mk()
    _qa()._apply_listed_at(lst, {"lastPublishedDate": "2026-08-11T20:29:10.000Z"})
    assert lst.listed_at == date(2026, 8, 11)


def test_qa_dead_listing_leaves_listed_at_unset():
    lst = _mk()
    _qa()._apply_listed_at(lst, {})
    assert lst.listed_at is None


def test_qa_rent_classifieds_use_the_hydrated_route():
    q = _qa()
    assert q._rent_detail_url("1", classified=True).endswith("/classificado/1/alugar")
    assert q._rent_detail_url("1", classified=False).endswith("/imovel/1")


# --- backfill ---------------------------------------------------------------

def test_listed_at_urls_try_stored_then_counterpart():
    q = _qa()
    rent = q._listed_at_urls("7", "https://www.quintoandar.com.br/imovel/7", "rent")
    assert rent[0].endswith("/imovel/7")
    assert rent[1].endswith("/classificado/7/alugar")
    assert len(rent) == 2  # a dead ad has no date on any route; don't burn a third
    buy = q._listed_at_urls("7", "https://www.quintoandar.com.br/classificado/7/comprar", "buy")
    assert buy[0].endswith("/classificado/7/comprar")
    assert buy[1].endswith("/imovel/7/comprar")


def test_fetch_listed_at_falls_through_to_the_second_route():
    q = _qa()
    seen = []

    def fake_detail(external_id, url=None):
        seen.append(url)
        if "classificado" not in (url or ""):
            return {"houseInfo": {}}  # unhydrated shell
        return {"houseInfo": {"lastPublishedDate": "2026-08-11T20:29:10.000Z"}}

    q._fetch_detail = fake_detail
    got = q.fetch_listed_at(
        external_id="7", url="https://www.quintoandar.com.br/imovel/7", kind="rent"
    )
    assert got == date(2026, 8, 11)
    assert len(seen) == 2


def test_backfill_skips_rows_that_already_have_a_date(monkeypatch):
    from rental_finder import pipeline

    rows = [
        {"page_id": "a", "source": "quintoandar", "external_id": "1", "url": "u1",
         "listed_at": "2026-01-01"},
        {"page_id": "b", "source": "quintoandar", "external_id": "2", "url": "u2",
         "listed_at": None},
        {"page_id": "c", "source": "nobody", "external_id": "3", "url": "u3",
         "listed_at": None},
    ]
    patched = []

    class _Store:
        def ensure_columns(self, kind): pass
        def list_alive(self, kind): return iter(rows)
        def list_gone(self, kind): return iter([])
        def update_listed_at(self, page_id, d): patched.append((page_id, d))

    class _Src:
        name = "quintoandar"
        def fetch_listed_at(self, *, external_id, url, kind):
            return date(2026, 6, 8)

    monkeypatch.setattr(pipeline, "NotionStore", lambda: _Store())
    monkeypatch.setattr(pipeline, "enabled_sources", lambda mode=None: [_Src()])
    stats = pipeline.backfill_listed_at(kind="rent")
    assert patched == [("b", date(2026, 6, 8))]
    assert stats == {
        "examined": 3, "skipped": 1, "no_source": 1, "no_date": 0, "updated": 1
    }
