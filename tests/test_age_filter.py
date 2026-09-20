from datetime import date

import pytest

from rental_finder import condos, pipeline
from rental_finder.condos import (
    CondoRegistry,
    listing_too_old,
    registry_too_old,
    year_too_old,
)
from rental_finder.config import settings
from rental_finder.models import Listing

CUR = date.today().year


def _mk(**kw) -> Listing:
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
        sqm=90.0,
        pets=True,
        furnished=False,
        lat=None,
        lng=None,
        mode="buy",
        property_type="Apartamento",
    )
    base.update(kw)
    return Listing(**base)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(settings, "condo_age_filter_enabled", True)
    monkeypatch.setattr(settings, "condo_max_age_years", 40)
    monkeypatch.setattr(settings, "condo_age_min_confidence", "medium")


def test_old_building_rejected():
    assert year_too_old(CUR - 41, "high")


def test_exactly_at_cap_kept():
    assert not year_too_old(CUR - 40, "high")


def test_unknown_year_kept():
    assert not year_too_old(None, "high")


def test_low_confidence_year_never_drops():
    """An uncorroborated year must not throw a building away."""
    assert not year_too_old(CUR - 60, "estimated")
    assert not year_too_old(CUR - 60, "not_found")


def test_gate_disabled(monkeypatch):
    monkeypatch.setattr(settings, "condo_age_filter_enabled", False)
    assert not year_too_old(1900, "high")


def test_zero_cap_disables(monkeypatch):
    monkeypatch.setattr(settings, "condo_max_age_years", 0)
    assert not year_too_old(1900, "high")


def test_listing_too_old_uses_listing_fields():
    lst = _mk()
    lst.apply_built_year(CUR - 50)
    lst.condo_confidence = "high"
    assert listing_too_old(lst)
    lst.apply_built_year(CUR - 5)
    assert not listing_too_old(lst)


# --- registry tier (the free one) -----------------------------------------


@pytest.fixture
def reg(tmp_path, monkeypatch):
    r = CondoRegistry(path=tmp_path / "condos.json", overrides=tmp_path / "none.csv")
    monkeypatch.setattr(condos, "_registry", r)
    return r


def test_registry_too_old(reg):
    reg.learn(
        "rua x|10",
        built_year=CUR - 55,
        source="claude",
        year_confidence="high",
    )
    assert registry_too_old("rua x|10")


def test_registry_unknown_building_kept(reg):
    assert not registry_too_old("rua nunca vista|1")
    assert not registry_too_old("")


def test_registry_blocked_building_kept(reg):
    """A blocked building has no trustworthy year, so it survives the gate."""
    reg.learn("rua y|2", built_year=1950, source="manual", year_confidence="high")
    reg.learn("rua y|2", source="manual", blocked=True)
    assert not registry_too_old("rua y|2")


# --- pipeline wiring ------------------------------------------------------


def test_prefilter_drops_known_old_building(reg):
    reg.learn(
        "rua arandu|222",
        built_year=CUR - 45,
        source="claude",
        year_confidence="high",
    )
    lst = _mk(condo_street="Rua Arandu", condo_number="222")
    assert not pipeline._prefilter_buy(lst)


def test_prefilter_keeps_recent_building(reg):
    reg.learn(
        "rua arandu|222",
        built_year=CUR - 5,
        source="claude",
        year_confidence="high",
    )
    lst = _mk(condo_street="Rua Arandu", condo_number="222")
    assert pipeline._prefilter_buy(lst)


class _FakeStore:
    def __init__(self):
        self.upserted = []
        self.dropped = []

    def upsert_listing(self, lst, kind="rent"):
        self.upserted.append(lst.external_id)
        return "page"

    def drop_listing(self, lst, kind="rent"):
        self.dropped.append(lst.external_id)
        return True


def test_finalize_drops_old_and_archives(monkeypatch, reg):
    monkeypatch.setattr(pipeline, "_wants_condo", lambda lst, kind: False)
    store = _FakeStore()
    lst = _mk()
    lst.apply_built_year(CUR - 50)
    lst.condo_confidence = "high"
    upserted, drop, reason = pipeline._finalize(lst, store, date.today(), "buy")
    assert (upserted, reason) == (False, "age")
    assert store.upserted == [] and store.dropped == ["1"]


def test_finalize_keeps_recent(monkeypatch, reg):
    monkeypatch.setattr(pipeline, "_wants_condo", lambda lst, kind: False)
    store = _FakeStore()
    lst = _mk()
    lst.apply_built_year(CUR - 3)
    lst.condo_confidence = "high"
    upserted, drop, reason = pipeline._finalize(lst, store, date.today(), "buy")
    assert (upserted, reason) == (True, "")
    assert store.upserted == ["1"]


def test_finalize_keeps_unknown_year(monkeypatch, reg):
    monkeypatch.setattr(pipeline, "_wants_condo", lambda lst, kind: False)
    store = _FakeStore()
    upserted, _, reason = pipeline._finalize(_mk(), store, date.today(), "buy")
    assert (upserted, reason) == (True, "")
