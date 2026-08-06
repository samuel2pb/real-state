from datetime import date, timedelta

import pytest

from rental_finder import condos
from rental_finder.condos import (
    CondoRegistry,
    clean_condo_name,
    condo_key,
    normalize_cep,
    search_name,
    split_street_number,
)
from rental_finder.models import Listing


def _mk(**kw) -> Listing:
    base = dict(
        source="quintoandar",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        price_rent=0.0,
        price_total=0.0,
        bedrooms=2,
        bathrooms=2,
        suites=1,
        parking=1,
        sqm=80.0,
        pets=False,
        furnished=None,
        lat=None,
        lng=None,
        mode="buy",
    )
    base.update(kw)
    return Listing(**base)


# --- age ------------------------------------------------------------------


def test_age_derived_from_year():
    lst = _mk()
    lst.apply_built_year(1988)
    assert lst.condo_age_years == date.today().year - 1988


def test_age_null_when_year_unknown():
    lst = _mk()
    lst.apply_built_year(None)
    assert (lst.built_year, lst.condo_age_years) == (None, None)


def test_future_launch_never_negative():
    lst = _mk()
    lst.apply_built_year(date.today().year + 3)
    assert lst.condo_age_years == 0


# --- key ------------------------------------------------------------------


@pytest.mark.parametrize(
    "street,num,expected",
    [
        ("Avenida Ibijaú", "191", "avenida ibijau|191"),
        ("Av. Ibijaú", 191, "avenida ibijau|191"),          # abbreviation
        ("Avenida Ibijau", "191-A", "avenida ibijau|191"),  # unit suffix
        ("Rua Gaivota", "1027", "rua gaivota|1027"),
        ("Al. dos Jurupis", "900", "alameda dos jurupis|900"),
        ("", "191", ""),
        ("Avenida Ibijaú", "", ""),
        ("Avenida Ibijaú", "s/n", ""),
    ],
)
def test_condo_key(street, num, expected):
    assert condo_key(street, num) == expected


def test_number_packed_into_street():
    # QuintoAndar serves street="Avenida Agami 333" with number=None
    assert split_street_number("Avenida Agami 333", None) == ("Avenida Agami", "333")
    assert split_street_number("Avenida Ibijaú", "191") == ("Avenida Ibijaú", "191")
    assert condo_key("Avenida Agami 333", None) == "avenida agami|333"


def test_same_building_one_key_across_both_qa_routes():
    # /imovel serves a CEP, /classificado serves none — the key must not diverge
    assert condo_key("Alameda Jauaperi", "1120") == condo_key("Al. Jauaperi", "1120")


@pytest.mark.parametrize(
    "raw,expected",
    [("04524-020", "04524020"), ("04524020", "04524020"), ("0452402", ""), ("", "")],
)
def test_normalize_cep(raw, expected):
    assert normalize_cep(raw) == expected


# --- name cleanup ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("do Edifício Twins Towers - (Moema, SP)", "Edifício Twins Towers"),
        ("ILHA DE CRETA - Campo Belo", "Ilha de Creta"),
        ("Maison D Or", "Maison D Or"),
        ("Spotlight Campo Belo", "Spotlight Campo Belo"),
        ("", ""),
    ],
)
def test_clean_condo_name(raw, expected):
    assert clean_condo_name(raw) == expected


def test_search_name_drops_generic_building_word():
    assert search_name("Edifício Rousseau") == "Rousseau"
    assert search_name("Condomínio Mustique") == "Mustique"
    assert search_name("Maison D Or") == "Maison D Or"


# --- registry -------------------------------------------------------------


@pytest.fixture
def reg(tmp_path):
    return CondoRegistry(
        path=tmp_path / "condos.json", overrides=tmp_path / "none.csv"
    )


def test_registry_roundtrip_and_persist(reg, tmp_path):
    reg.learn("avenida ibijau|191", name="Maison D Or", built_year=1988, source="quintoandar")
    again = CondoRegistry(path=tmp_path / "condos.json", overrides=tmp_path / "none.csv")
    info = again.lookup("avenida ibijau|191")
    assert (info.name, info.built_year, info.year_source) == (
        "Maison D Or",
        1988,
        "quintoandar",
    )


def test_learn_never_downgrades(reg):
    reg.learn("k|1", built_year=1988, source="quintoandar")
    reg.learn("k|1", built_year=1975, source="search")
    assert reg.lookup("k|1").built_year == 1988
    reg.learn("k|1", built_year=1990, source="manual")
    assert reg.lookup("k|1").built_year == 1990


def test_learn_never_nulls_a_known_value(reg):
    reg.learn("k|1", name="Mustique", built_year=2005, source="quintoandar")
    reg.learn("k|1", built_year=None, source="search", searched_at="2026-08-06")
    info = reg.lookup("k|1")
    assert (info.built_year, info.name) == (2005, "Mustique")


def test_empty_key_is_ignored(reg):
    reg.learn("", name="X", source="quintoandar")
    assert reg.stats()["buildings"] == 0


def test_needs_search_respects_cooldown(reg):
    assert reg.needs_search("k|1") is True          # unknown building
    reg.learn("k|1", built_year=2005, source="search")
    assert reg.needs_search("k|1") is False         # already has a year
    reg.learn("k|2", source="search", searched_at=date.today().isoformat())
    assert reg.needs_search("k|2") is False         # miss, still in cooldown
    old = (date.today() - timedelta(days=400)).isoformat()
    reg.learn("k|3", source="search", searched_at=old)
    assert reg.needs_search("k|3") is True          # cooldown expired


def test_claim_search_is_exclusive(reg):
    # two listings in the same building must not both pay for a query
    assert reg.claim_search("k|1") is True
    assert reg.claim_search("k|1") is False
    reg.release_search("k|1")
    assert reg.claim_search("k|1") is True


def test_claim_search_refuses_a_known_year(reg):
    reg.learn("k|1", built_year=2005, source="quintoandar")
    assert reg.claim_search("k|1") is False


def test_claim_search_refuses_empty_key(reg):
    assert reg.claim_search("") is False


def test_overrides_win(tmp_path):
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "key,name,built_year\navenida ibijau|191,Maison D'Or,1990\n", encoding="utf-8"
    )
    reg = CondoRegistry(path=tmp_path / "condos.json", overrides=csv_path)
    reg.learn("avenida ibijau|191", name="Maison D Or", built_year=1988, source="quintoandar")
    info = reg.lookup("avenida ibijau|191")
    assert (info.built_year, info.name, info.year_source) == (
        1990,
        "Maison D'Or",
        "manual",
    )


# --- resolve --------------------------------------------------------------


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    reg = CondoRegistry(path=tmp_path / "condos.json", overrides=tmp_path / "none.csv")
    monkeypatch.setattr(condos, "_registry", reg)
    monkeypatch.setattr(condos.settings, "condo_search_enabled", False)
    return reg


def test_resolve_reads_registry(isolated_registry):
    isolated_registry.learn(
        "avenida ibijau|191", name="Maison D Or", built_year=1988, source="quintoandar"
    )
    lst = _mk(condo_key="avenida ibijau|191")
    condos.resolve_condo(lst)
    assert lst.condo_name == "Maison D Or"
    assert lst.built_year == 1988
    assert lst.condo_age_years == date.today().year - 1988
    assert lst.condo_source == "quintoandar"


def test_resolve_without_key_is_a_noop(isolated_registry):
    lst = _mk()
    condos.resolve_condo(lst)
    assert (lst.condo_name, lst.built_year) == ("", None)


def test_resolve_does_not_search_when_disabled(isolated_registry, monkeypatch):
    called = []
    monkeypatch.setattr(
        "rental_finder.condo_search.search_year",
        lambda **kw: called.append(kw),
    )
    lst = _mk(condo_key="avenida ibijau|191", condo_name_raw="Rousseau")
    condos.resolve_condo(lst)
    assert called == []


def test_run_budget_caps_searches(isolated_registry, monkeypatch):
    monkeypatch.setattr(condos.settings, "condo_search_enabled", True)
    monkeypatch.setattr(condos.settings, "condo_search_max_per_run", 2)
    condos.reset_run_budget()
    calls = []

    def _fake(**kw):
        calls.append(kw)
        return None

    monkeypatch.setattr("rental_finder.condo_search.search_year", _fake)
    for i in range(5):
        condos.resolve_condo(
            _mk(condo_key=f"0452402{i}|1", condo_name_raw=f"Cond {i}")
        )
    assert len(calls) == 2


def test_is_apartment():
    assert condos.is_apartment(_mk(property_type="Apartamento")) is True
    assert condos.is_apartment(_mk(property_type="APARTMENT")) is True
    assert condos.is_apartment(_mk(property_type="Casa")) is False
    assert condos.is_apartment(_mk()) is False


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("Maison D Or", True),
        ("Ed", False),        # too short to be a name
        ("8307", False),      # a bare number seen live at Rua Bacaetava 181
        ("  ", False),
        ("", False),
        ("D'Artagnan", True),
    ],
)
def test_looks_like_name(raw, ok):
    assert condos.looks_like_name(raw) is ok


def test_numeric_name_is_not_stored(reg):
    reg.learn("rua bacaetava|181", name="8307", source="quintoandar")
    assert reg.lookup("rua bacaetava|181").name == ""


def test_numeric_name_is_not_searched(isolated_registry, monkeypatch):
    monkeypatch.setattr(condos.settings, "condo_search_enabled", True)
    condos.reset_run_budget()
    calls = []
    monkeypatch.setattr(
        "rental_finder.condo_search.search_year", lambda **kw: calls.append(kw)
    )
    condos.resolve_condo(_mk(condo_key="rua bacaetava|181", condo_name_raw="8307"))
    assert calls == []
