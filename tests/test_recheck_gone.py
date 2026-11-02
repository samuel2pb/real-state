from datetime import date

import pytest

from rental_finder import pipeline
from rental_finder.config import settings
from rental_finder.models import Listing
from rental_finder.sources.quintoandar import QuintoAndarSource

CUR = date.today().year


@pytest.fixture(autouse=True)
def _filters(monkeypatch):
    for k, v in {
        "rent_price_min": 3000,
        "rent_price_max": 9000,
        "rent_bedrooms_min": 2,
        "rent_bedrooms_max": 4,
        "rent_bathrooms_min": 2,
        "rent_suites_min": 1,
        "rent_sqm_min": 70,
        "rent_parking_min": 1,
        "rent_pets_required": True,
        "rent_furnished_allowed": False,
        "rent_property_types": "apartment,condo_house",
        "buy_price_min": 500000,
        "buy_price_max": 1500000,
        "buy_condo_max": 0,
        "buy_bedrooms_min": 2,
        "buy_bedrooms_max": 4,
        "buy_bathrooms_min": 2,
        "buy_suites_min": 1,
        "buy_sqm_min": 70,
        "buy_parking_min": 1,
        "buy_pets_required": True,
        "buy_furnished_allowed": False,
        "buy_property_types": "apartment",
        "work_max_distance_km": 10.0,
        "condo_enrich_enabled": False,
        "condo_age_filter_enabled": True,
        "condo_max_age_years": 40,
        "condo_age_min_confidence": "medium",
        "condo_age_require_known": True,
        "pool_workers": 2,
    }.items():
        monkeypatch.setattr(settings, k, v)


def _qa() -> QuintoAndarSource:
    return QuintoAndarSource.__new__(QuintoAndarSource)


def _detail(context: str = "RENT", status: str = "publicado", **hi) -> dict:
    base = {
        "bedrooms": 3,
        "bathrooms": 2,
        "suites": 1,
        "parkingSpaces": 1,
        "area": 90,
        "rentPrice": 4000,
        "totalCost": 5900,
        "salePrice": 990000,
        "condoPrice": 1500,
        "iptu": 120,
        "type": "Apartamento",
        "acceptsPets": True,
        "hasFurniture": False,
        "status": "publicado",
        "address": {"street": "Rua X", "neighborhood": "Paraíso"},
        "listings": [
            {
                "businessContext": context,
                "status": status,
                "lastPublicationDate": "2026-10-01T10:00:00.000+0000",
            }
        ],
    }
    base.update(hi)
    return {"houseInfo": base, "markers": {"lat": -23.58, "lng": -46.68}}


# A dead ad still answers with a hydrated-looking dict, every number zeroed.
_SHELL = {
    "houseInfo": {
        "bedrooms": 0,
        "bathrooms": "",
        "rentPrice": 0,
        "salePrice": None,
        "status": "",
        "listings": [],
    },
    "markers": {},
}


def test_shell_is_not_published():
    assert not _qa()._published(_SHELL["houseInfo"], "rent")


def test_published_is_per_business_context():
    hi = _detail()["houseInfo"]
    hi["listings"] = [
        {"businessContext": "SALE", "status": "publicado"},
        {"businessContext": "RENT", "status": "unpublished"},
    ]
    assert _qa()._published(hi, "buy")
    assert not _qa()._published(hi, "rent")


def test_fetch_listing_builds_a_full_rent_listing():
    q = _qa()
    q._fetch_detail = lambda hid, url=None: _detail()
    lst = q.fetch_listing(
        external_id="7", url="https://www.quintoandar.com.br/imovel/7",
        kind="rent", neighborhood="Moema",
    )
    assert lst is not None
    assert lst.neighborhood == "Moema"  # the stored row's name wins
    assert lst.price_rent == 4000
    assert lst.price_total == 4000 + 1500 + 120  # same rule as _enrich_rent
    assert (lst.bedrooms, lst.bathrooms, lst.suites, lst.parking) == (3, 2, 1, 1)
    assert lst.pets is True and lst.furnished is False
    assert (lst.lat, lst.lng) == (-23.58, -46.68)
    assert lst.listed_at == date(2026, 10, 1)


def test_fetch_listing_falls_through_to_the_classified_route():
    q = _qa()
    seen = []

    def fake(hid, url=None):
        seen.append(url)
        return _detail() if "classificado" in url else _SHELL

    q._fetch_detail = fake
    lst = q.fetch_listing(
        external_id="7", url="https://www.quintoandar.com.br/imovel/7", kind="rent"
    )
    assert lst is not None
    assert lst.url.endswith("/classificado/7/alugar")
    assert len(seen) == 2


def test_recheck_dead_shell_is_not_alive():
    q = _qa()
    q._fetch_detail = lambda hid, url=None: _SHELL
    assert q.recheck(external_id="7", url="", kind="rent") == (False, None)


def test_recheck_unpublished_context_is_not_alive():
    q = _qa()
    q._fetch_buy_detail = lambda hid, url=None: _detail("SALE", "unpublished")
    assert q.recheck(external_id="7", url="", kind="buy") == (False, None)


def test_recheck_tries_the_counterpart_route():
    q = _qa()
    q._fetch_detail = lambda hid, url=None: (
        _detail() if "classificado" in url else _SHELL
    )
    got = q.recheck(
        external_id="7", url="https://www.quintoandar.com.br/imovel/7", kind="rent"
    )
    assert got == (True, 5900.0)


def test_fetch_listing_none_when_unpublished_everywhere():
    q = _qa()
    q._fetch_buy_detail = lambda hid, url=None: _detail("SALE", "unpublished")
    assert q.fetch_listing(external_id="7", url="", kind="buy") is None


def test_fetch_listing_buy_sets_sale_price():
    q = _qa()
    q._fetch_buy_detail = lambda hid, url=None: _detail("SALE")
    lst = q.fetch_listing(external_id="7", url="", kind="buy")
    assert lst.mode == "buy"
    assert lst.price_sale == 990000
    assert lst.condo_fee == 1500


@pytest.mark.parametrize(
    "ptype,kind,ok",
    [
        ("Apartamento", "rent", True),
        ("CasaCondominio", "rent", True),
        ("Casa de Condomínio", "rent", True),
        ("Casa", "rent", False),
        ("CasaCondominio", "buy", False),
        ("", "buy", True),
    ],
)
def test_type_allowed(ptype, kind, ok):
    lst = Listing(
        source="quintoandar", external_id="1", url="", neighborhood="",
        price_rent=0, price_total=0, bedrooms=0, bathrooms=0, suites=0,
        parking=0, sqm=0, pets=False, furnished=None, lat=None, lng=None,
        property_type=ptype,
    )
    assert pipeline._type_allowed(lst, kind) is ok


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.upserts: list[Listing] = []
        self.price_up: list = []
        self.price_down: list = []
        self.dropped: list = []

    def ensure_columns(self, kind): pass
    def prime_index(self, kind): pass
    def list_gone(self, kind): return iter(self.rows)
    def upsert_listing(self, lst, kind): self.upserts.append(lst)
    def mark_price_up(self, page_id, kind, price): self.price_up.append((page_id, price))

    def mark_price_down(self, page_id, kind, price, prev_price):
        self.price_down.append((page_id, price))

    def drop_listing(self, lst, kind): self.dropped.append(lst.external_id)


def _row(ext: str, source: str = "quintoandar") -> dict:
    return {
        "page_id": f"p{ext}", "source": source, "external_id": ext,
        "url": f"https://www.quintoandar.com.br/imovel/{ext}",
        "neighborhood": "Moema", "price": 5000,
    }


def _run(monkeypatch, rows, details, kind="rent", extra_sources=()):
    store = _Store(rows)
    q = _qa()

    def fake(hid, url=None):
        return details.get(hid, _SHELL)

    q._fetch_detail = fake
    q._fetch_buy_detail = fake
    monkeypatch.setattr(pipeline, "NotionStore", lambda: store)
    monkeypatch.setattr(pipeline, "work_coords", lambda: (-23.585, -46.68))
    monkeypatch.setattr(
        pipeline, "enabled_sources", lambda mode=None: [q, *extra_sources]
    )
    return store, pipeline.recheck_gone(kind=kind)


def test_recheck_gone_rent_reclassifies_each_case(monkeypatch):
    class _NoFetch:
        name = "zap"
        can_fetch_listing = False

        def recheck(self, *, external_id, url, kind):
            return (True, None)  # live, price unknown -> cannot judge filters

    rows = [_row("1"), _row("2"), _row("3"), _row("4"), _row("5"), _row("9", "zap")]
    details = {
        "1": _detail(),  # live, passes everything
        # "2" missing -> shell on both routes -> still gone
        "3": _detail(rentPrice=9500, condoPrice=0, iptu=0, totalCost=9500),
        "4": _detail(bedrooms=1),  # live but fails the matcher
        "5": _detail(type="Casa"),  # live but not a searched property type
    }
    store, stats = _run(monkeypatch, rows, details, extra_sources=[_NoFetch()])

    assert [lst.external_id for lst in store.upserts] == ["1"]
    revived = store.upserts[0]
    assert revived.status == "available"
    assert revived.last_seen == date.today()
    assert store.price_up == [("p3", 9500)]
    assert stats == {
        "examined": 6, "revived": 1, "price_up": 1, "price_drop": 0,
        "still_gone": 1, "rejected": 2, "aged_out": 0, "age_unknown": 0,
        "skipped": 1, "duplicates": 0, "errors": 0,
    }


def test_recheck_gone_rejects_listings_beyond_distance_cap(monkeypatch):
    far = _detail()
    far["markers"] = {"lat": -23.0, "lng": -46.0}
    store, stats = _run(monkeypatch, [_row("1")], {"1": far})
    assert store.upserts == []
    assert stats["rejected"] == 1


def test_recheck_gone_buy_applies_age_gate(monkeypatch):
    # condo_enrich off -> no year is ever resolved -> require_known rejects,
    # and the row is trashed exactly as a cycle's _finalize would.
    store, stats = _run(monkeypatch, [_row("1")], {"1": _detail("SALE")}, kind="buy")
    assert store.upserts == []
    assert store.dropped == ["1"]
    assert stats["age_unknown"] == 1


def test_recheck_gone_buy_revives_young_building(monkeypatch):
    def young(lst):
        lst.apply_built_year(CUR - 5)
        lst.condo_confidence = "high"

    monkeypatch.setattr(settings, "condo_enrich_enabled", True)
    monkeypatch.setattr(pipeline, "is_apartment", lambda lst: True)
    monkeypatch.setattr(pipeline, "resolve_condo", young)
    monkeypatch.setattr(QuintoAndarSource, "_apply_condo", lambda self, lst, hi: None)
    store, stats = _run(monkeypatch, [_row("1")], {"1": _detail("SALE")}, kind="buy")
    assert [lst.external_id for lst in store.upserts] == ["1"]
    assert stats["revived"] == 1


def test_recheck_gone_checks_a_repeated_listing_once(monkeypatch):
    rows = [_row("1"), {**_row("1"), "page_id": "p1-dup"}]
    store, stats = _run(monkeypatch, rows, {"1": _detail()})
    assert len(store.upserts) == 1
    assert stats["examined"] == 1
    assert stats["duplicates"] == 1
    assert stats["revived"] == 1


def _dup(page_id, status, last_seen="", first_seen="", created=""):
    return {
        "page_id": page_id, "source": "quintoandar", "external_id": "1",
        "status": status, "last_seen": last_seen, "first_seen": first_seen,
        "created": created,
    }


def test_pick_keeper_prefers_alive_then_recent():
    rows = [
        _dup("a", "gone", "2026-10-15"),
        _dup("b", "available", "2026-09-01"),
        _dup("c", "available", "2026-10-10"),
        _dup("d", "available", ""),
    ]
    assert pipeline._pick_keeper(rows)["page_id"] == "c"


def test_pick_keeper_breaks_ties_on_oldest_row():
    rows = [
        _dup("new", "gone", "2026-10-01", created="2026-10-02T00:00:00Z"),
        _dup("old", "gone", "2026-10-01", created="2026-05-02T00:00:00Z"),
    ]
    assert pipeline._pick_keeper(rows)["page_id"] == "old"


class _DupStore:
    def __init__(self, groups):
        self.groups = groups
        self.archived: list[str] = []
        self.first_seen: list = []

    def duplicate_groups(self, kind): return self.groups
    def archive_page(self, page_id): self.archived.append(page_id)
    def set_first_seen(self, page_id, iso): self.first_seen.append((page_id, iso))


def test_dedupe_rows_dry_run_writes_nothing(monkeypatch):
    store = _DupStore([[_dup("a", "gone"), _dup("b", "available")]])
    monkeypatch.setattr(pipeline, "NotionStore", lambda: store)
    stats = pipeline.dedupe_rows(kind="rent")
    assert stats == {"groups": 1, "trashed": 0, "first_seen_moved": 0}
    assert store.archived == [] and store.first_seen == []


def test_dedupe_rows_apply_trashes_extras_and_keeps_earliest_first_seen(monkeypatch):
    store = _DupStore([[
        _dup("a", "gone", "2026-10-01", first_seen="2026-06-01"),
        _dup("b", "available", "2026-10-16", first_seen="2026-08-01"),
        _dup("c", "gone", "2026-09-01", first_seen="2026-07-01"),
    ]])
    monkeypatch.setattr(pipeline, "NotionStore", lambda: store)
    stats = pipeline.dedupe_rows(kind="rent", apply=True)
    assert sorted(store.archived) == ["a", "c"]
    assert store.first_seen == [("b", "2026-06-01")]
    assert stats == {"groups": 1, "trashed": 2, "first_seen_moved": 1}
