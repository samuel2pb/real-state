"""The buy cycle's cost is detail fetches and Notion lookups; these guard both.

Enrich is the long pole: one detail fetch per listing, each sleeping
http_min/max_delay_sec first. Two things cut it — not fetching for listings the
search hit already disqualifies, and not letting a multi-minute condo lookup sit
on a worker the remaining fetches are queued behind.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest

from rental_finder import pipeline
from rental_finder.models import Listing
from rental_finder.notion_store import NotionStore
from rental_finder.sources.quintoandar import QuintoAndarSource

WORK = (-23.585, -46.675)  # Itaim Bibi


def _mk(**over) -> Listing:
    base = dict(
        source="quintoandar",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        price_rent=0.0,
        price_total=900000.0,
        price_sale=900000.0,
        bedrooms=3,
        bathrooms=2,
        suites=1,
        parking=1,
        sqm=100.0,
        pets=True,
        pets_known=True,
        furnished=False,
        lat=-23.60,
        lng=-46.66,
        mode="buy",
        property_type="Apartamento",
    )
    base.update(over)
    return Listing(**base)


@pytest.fixture
def buy_settings(monkeypatch):
    s = pipeline.settings
    for k, v in (
        ("buy_price_min", 500000),
        ("buy_price_max", 1200000),
        ("buy_bedrooms_min", 2),
        ("buy_bedrooms_max", 3),
        ("buy_bathrooms_min", 2),
        ("buy_sqm_min", 70),
        ("buy_pets_required", True),
        ("buy_furnished_allowed", True),
        ("buy_prefilter_enabled", True),
    ):
        monkeypatch.setattr(s, k, v)
    return s


# --- prefilter -------------------------------------------------------------


def test_pets_known_and_refused_is_dropped_before_any_fetch(buy_settings):
    assert pipeline._prefilter_buy(_mk(pets=False, pets_known=True)) is False


def test_pets_unknown_still_earns_its_detail_fetch(buy_settings):
    """An empty amenities list means the payload said nothing, not "no pets"."""
    assert pipeline._prefilter_buy(_mk(pets=False, pets_known=False)) is True


def test_prefilter_rejects_on_price(buy_settings):
    assert pipeline._prefilter_buy(_mk(price_sale=2_000_000.0)) is False


@pytest.mark.parametrize("field", ["bedrooms", "bathrooms", "sqm"])
def test_zero_reads_as_not_stated_not_as_zero(buy_settings, field):
    """A source that omits a field must not have its listings thrown away."""
    assert pipeline._prefilter_buy(_mk(**{field: 0})) is True


def test_prefilter_never_rejects_what_the_real_filters_would_keep(buy_settings):
    """Prefilter is an early-out for the same rules, never a stricter one."""
    keeper = _mk()
    assert pipeline._prefilter_buy(keeper) is True
    assert pipeline._matches_buy_filters(keeper) is True


def test_search_hit_carries_the_pets_flag():
    """The amenity code is what lets the prefilter skip the detail fetch."""
    src = QuintoAndarSource()
    raw = {
        "salePrice": 900000,
        "bedrooms": 3,
        "bathrooms": 2,
        "area": 100,
        "parkingSpots": 1,
        "type": "Apartamento",
        "amenities": ["PODE_TER_ANIMAIS_DE_ESTIMACAO", "BOX"],
    }
    lst = src._parse_buy_search_hit("1", raw, "Moema")
    assert lst.pets is True and lst.pets_known is True

    raw["amenities"] = ["BOX"]
    assert src._parse_buy_search_hit("1", raw, "Moema").pets is False

    del raw["amenities"]
    silent = src._parse_buy_search_hit("1", raw, "Moema")
    assert silent.pets is False and silent.pets_known is False


# --- condo lookups off the enrich pool -------------------------------------


class _Store:
    def __init__(self):
        self.upserted = []

    def upsert_listing(self, lst, kind):
        self.upserted.append(lst)


class _Src:
    name = "quintoandar"

    def __init__(self, listings):
        self._listings = listings

    def search_tasks(self, neighborhoods, kind):
        return [lambda: list(self._listings)]

    def enrich(self, lst):
        pass


def test_condo_lookups_do_not_run_on_the_enrich_pool(monkeypatch, buy_settings):
    """A lookup is minutes long; it must not occupy a detail-fetch worker."""
    monkeypatch.setattr(pipeline.settings, "condo_enrich_enabled", True)
    monkeypatch.setattr(pipeline.settings, "work_max_distance_km", 5.5)
    monkeypatch.setattr(pipeline.settings, "pool_workers", 4)
    monkeypatch.setattr(pipeline.settings, "condo_pool_workers", 2)
    monkeypatch.setattr(pipeline, "is_apartment", lambda lst: True)

    enrich_threads: set[str] = set()
    condo_threads: set[str] = set()

    def _enrich(lst):
        enrich_threads.add(threading.current_thread().name)

    monkeypatch.setattr(
        _Src, "enrich", lambda self, lst: _enrich(lst), raising=False
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_condo",
        lambda lst: condo_threads.add(threading.current_thread().name),
    )

    listings = [_mk(external_id=str(i)) for i in range(6)]
    store = _Store()
    sc, seen = pipeline._run_source(
        _Src(listings), store, WORK, date.today(), kind="buy"
    )

    assert sc["upserted"] == 6
    assert len(store.upserted) == 6
    assert condo_threads, "condo lookups never ran"
    assert not (condo_threads & enrich_threads)


def test_prefiltered_listings_are_never_enriched(monkeypatch, buy_settings):
    monkeypatch.setattr(pipeline.settings, "condo_enrich_enabled", False)
    monkeypatch.setattr(pipeline.settings, "work_max_distance_km", 5.5)

    enriched: list[str] = []
    monkeypatch.setattr(
        _Src,
        "enrich",
        lambda self, lst: enriched.append(lst.external_id),
        raising=False,
    )

    listings = [
        _mk(external_id="keep"),
        _mk(external_id="nopets", pets=False, pets_known=True),
    ]
    sc, seen = pipeline._run_source(
        _Src(listings), _Store(), WORK, date.today(), kind="buy"
    )

    assert enriched == ["keep"]
    assert sc == {
        "fetched": 2,
        "prefiltered": 1,
        "kept": 1,
        "upserted": 1,
        "price_drop": 0,
    }
    # A prefiltered listing is still 'seen', or reconcile would mark it gone.
    assert seen == {"keep", "nopets"}


# --- Notion page index -----------------------------------------------------


class _CountingClient:
    """Counts data_sources.query calls; that is the cost the index removes."""

    def __init__(self, pages):
        self.queries = 0
        self.updated = []
        self.created = []
        outer = self

        def _query(_s, **kw):
            outer.queries += 1
            if "filter" in kw:  # per-listing lookup
                return {"results": []}
            return {"results": pages, "has_more": False}

        self.data_sources = type("D", (), {"query": _query})()
        self.pages = type(
            "P",
            (),
            {
                "update": lambda _s, page_id, properties: outer.updated.append(page_id),
                "create": lambda _s, **kw: (
                    outer.created.append(kw["properties"]) or {"id": "new", "properties": {}}
                ),
            },
        )()


def _store(pages) -> NotionStore:
    st = NotionStore.__new__(NotionStore)
    st.client = _CountingClient(pages)
    st._ds_cache = {"buy": "ds"}
    st._missing_props = set()
    st._schema_ensured = set()
    st._index = {}
    st._lock = threading.Lock()
    st._data_source_id = lambda k: "ds"
    return st


def _page(page_id, source, ext, price):
    return {
        "id": page_id,
        "properties": {
            "ExternalID": {"rich_text": [{"plain_text": ext}]},
            "Source": {"select": {"name": source}},
            "Price": {"number": price},
        },
    }


def test_primed_index_costs_one_query_for_the_whole_base():
    st = _store([_page("p1", "quintoandar", "1", 900000)])
    assert st.prime_index("buy") == 1
    assert st.client.queries == 1

    st.upsert_listing(_mk(), kind="buy")

    assert st.client.updated == ["p1"], "found the row without a lookup query"
    assert st.client.queries == 1, "index was primed, no per-listing query"


def test_index_miss_creates_and_stays_findable():
    """A miss on a complete snapshot means the row is new; the create registers."""
    st = _store([])
    st.prime_index("buy")
    st.upsert_listing(_mk(external_id="new-id"), kind="buy")
    assert len(st.client.created) == 1

    st.upsert_listing(_mk(external_id="new-id"), kind="buy")
    assert len(st.client.created) == 1, "second pass would have duplicated the row"
    assert st.client.updated == ["new"]


def test_without_priming_the_per_listing_query_still_runs(monkeypatch):
    """Priming is an optimisation; an unprimed store must stay correct."""
    monkeypatch.setattr("rental_finder.notion_store.settings.notion_page_index", False)
    st = _store([])
    assert st.prime_index("buy") == 0
    assert st.client.queries == 0

    st.upsert_listing(_mk(), kind="buy")
    assert st.client.queries == 1, "fell back to querying for the page"
    assert len(st.client.created) == 1
