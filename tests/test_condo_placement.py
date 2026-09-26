"""A condo lookup is an agent run; only listings bound for Notion may spend one."""

from __future__ import annotations

from datetime import date

import pytest

from rental_finder import pipeline
from rental_finder.models import Listing

WORK = (-23.585, -46.675)  # Itaim Bibi


def _mk(lat: float, lng: float) -> Listing:
    return Listing(
        source="quintoandar",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        price_rent=0.0,
        price_total=900000.0,
        bedrooms=3,
        bathrooms=2,
        suites=1,
        parking=1,
        sqm=100.0,
        pets=False,
        furnished=None,
        lat=lat,
        lng=lng,
        mode="buy",
        property_type="Apartamento",
        condo_key="rua estela|755",
    )


class _Src:
    def enrich(self, lst):
        pass


class _Store:
    def __init__(self):
        self.upserted = []

    def upsert_listing(self, lst, kind):
        self.upserted.append(lst)


@pytest.fixture
def lookups(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "resolve_condo", lambda lst: calls.append(lst))
    monkeypatch.setattr(pipeline.settings, "condo_enrich_enabled", True)
    monkeypatch.setattr(pipeline.settings, "work_max_distance_km", 5.5)
    # The stub resolver finds no year; this module is about *when* the lookup
    # runs, not what the age gate does with its answer.
    monkeypatch.setattr(pipeline.settings, "condo_age_require_known", False)
    return calls


def _run(lst, matches=lambda lst: True):
    store = _Store()
    kept = pipeline._process_listing(
        lst, _Src(), store, WORK, date.today(), matches, "buy"
    )
    return kept, store


def test_listing_outside_the_radius_costs_no_lookup(lookups):
    kept, store = _run(_mk(-23.40, -46.40))  # ~40 km away
    assert kept is False and store.upserted == []
    assert lookups == []


def test_listing_failing_the_filters_costs_no_lookup(lookups):
    kept, store = _run(_mk(-23.60, -46.66), matches=lambda lst: False)
    assert kept is False
    assert lookups == []


def test_listing_bound_for_notion_is_resolved_before_upsert(lookups):
    kept, store = _run(_mk(-23.60, -46.66))
    assert kept is True
    assert len(lookups) == 1
    assert store.upserted == lookups  # resolved first, then written
