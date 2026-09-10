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


def test_needs_search_gates(reg):
    assert reg.needs_search("k|1") is True          # unknown building
    reg.learn("k|1", built_year=2005, source="search")
    assert reg.needs_search("k|1") is False         # already has a year


def test_a_miss_is_parked_for_good(reg, monkeypatch):
    # the year is not in any snippet for this building; retrying burns a credit
    # on every single run for nothing
    monkeypatch.setattr(condos.settings, "condo_search_retry_misses", False)
    reg.learn("k|2", source="search", searched_at=date.today().isoformat())
    assert reg.needs_search("k|2") is False
    ancient = (date.today() - timedelta(days=4000)).isoformat()
    reg.learn("k|3", source="search", searched_at=ancient)
    assert reg.needs_search("k|3") is False         # age does not reopen it


def test_retry_misses_reenables_the_cooldown(reg, monkeypatch):
    monkeypatch.setattr(condos.settings, "condo_search_retry_misses", True)
    monkeypatch.setattr(condos.settings, "condo_search_retry_days", 90)
    reg.learn("k|2", source="search", searched_at=date.today().isoformat())
    assert reg.needs_search("k|2") is False         # still inside the window
    old = (date.today() - timedelta(days=400)).isoformat()
    reg.learn("k|3", source="search", searched_at=old)
    assert reg.needs_search("k|3") is True          # window expired


def test_clear_misses_reopens_only_the_yearless(reg):
    reg.learn("k|1", built_year=2005, source="search",
              searched_at=date.today().isoformat())
    reg.learn("k|2", source="search", searched_at=date.today().isoformat())
    reg.learn("k|3", source="search", searched_at=date.today().isoformat())
    assert reg.stats()["parked_misses"] == 2
    assert reg.clear_misses() == 2
    assert reg.stats()["parked_misses"] == 0
    assert reg.needs_search("k|2") is True
    assert reg.needs_search("k|1") is False         # has a year, still skipped
    assert reg.clear_misses() == 0                  # idempotent


def test_stats_split_parked_from_never_searched(reg):
    reg.learn("k|1", source="search", searched_at=date.today().isoformat())
    reg.learn("k|2", name="Sem busca ainda", source="quintoandar")
    s = reg.stats()
    assert (s["parked_misses"], s["never_searched"]) == (1, 1)


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
    # The tests below exercise the legacy SERP tier; the `claude` tier has its
    # own fixture further down.
    monkeypatch.setattr(condos.settings, "condo_search_provider", "serper")
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


# --- claude tier ----------------------------------------------------------


@pytest.fixture
def agent_registry(tmp_path, monkeypatch):
    reg = CondoRegistry(path=tmp_path / "condos.json", overrides=tmp_path / "none.csv")
    monkeypatch.setattr(condos, "_registry", reg)
    monkeypatch.setattr(condos.settings, "condo_search_enabled", True)
    monkeypatch.setattr(condos.settings, "condo_search_provider", "claude")
    monkeypatch.setattr(condos.settings, "condo_search_min_confidence", "medium")
    condos.reset_run_budget()
    return reg


def _finding(**kw):
    from rental_finder.condo_agent import CondoFinding

    return CondoFinding(**kw)


def test_split_key():
    assert condos.split_key("rua jose antonio coelho|603") == (
        "rua jose antonio coelho",
        "603",
    )
    assert condos.split_key("") == ("", "")


def test_agent_gets_the_address_out_of_the_key(agent_registry, monkeypatch):
    """A Notion backfill row carries no street; the SERP tier searched empty."""
    seen = {}

    def _fake(**kw):
        seen.update(kw)
        return _finding(built_year=1977, confidence="high", found_number="603")

    monkeypatch.setattr("rental_finder.condo_agent.lookup_condo", _fake)
    lst = _mk(condo_key="rua jose antonio coelho|603")
    condos.resolve_condo(lst)
    assert seen["street"] == "rua jose antonio coelho"
    assert seen["number"] == "603"
    assert lst.built_year == 1977
    assert lst.condo_source == "claude"
    assert lst.condo_confidence == "high"


def test_unnamed_building_is_still_looked_up(agent_registry, monkeypatch):
    """The SERP tier bailed without a name. Address alone is enough now."""
    calls = []
    monkeypatch.setattr(
        "rental_finder.condo_agent.lookup_condo",
        lambda **kw: calls.append(kw) or _finding(
            built_year=1982,
            name="Luci e Lea",
            confidence="medium",
            found_number="962",
        ),
    )
    lst = _mk(condo_key="travessa humberto i|962")
    condos.resolve_condo(lst)
    assert len(calls) == 1
    assert lst.condo_name == "Luci e Lea"
    assert lst.built_year == 1982


def test_low_confidence_year_is_recorded_but_not_stored(agent_registry, monkeypatch):
    monkeypatch.setattr(
        "rental_finder.condo_agent.lookup_condo",
        lambda **kw: _finding(
            built_year=1978, confidence="estimated", found_number="928"
        ),
    )
    lst = _mk(condo_key="rua humberto i|928")
    condos.resolve_condo(lst)
    assert lst.built_year is None
    assert lst.condo_confidence == "estimated"
    assert agent_registry.lookup("rua humberto i|928").built_year is None


def test_unreachable_resolver_is_not_parked(agent_registry, monkeypatch):
    """None means we never got an answer — retry it, do not park it."""
    monkeypatch.setattr("rental_finder.condo_agent.lookup_condo", lambda **kw: None)
    lst = _mk(condo_key="rua estela|755")
    condos.resolve_condo(lst)
    assert agent_registry.needs_search("rua estela|755") is True


def test_a_miss_is_parked(agent_registry, monkeypatch):
    monkeypatch.setattr(
        "rental_finder.condo_agent.lookup_condo",
        lambda **kw: _finding(confidence="not_found", notes="no source states it"),
    )
    lst = _mk(condo_key="rua jose antonio coelho|750")
    condos.resolve_condo(lst)
    assert agent_registry.needs_search("rua jose antonio coelho|750") is False
    assert (
        agent_registry.lookup("rua jose antonio coelho|750").year_notes
        == "no source states it"
    )


def test_quintoandar_name_outranks_the_agent(reg):
    reg.learn("k|1", name="Maison D Or", source="quintoandar")
    reg.learn("k|1", name="Maison Dor Residence", source="claude")
    assert reg.lookup("k|1").name == "Maison D Or"


def test_agent_year_outranks_quintoandar(reg):
    reg.learn("k|1", built_year=2023, source="quintoandar")
    reg.learn("k|1", built_year=1977, source="claude", year_confidence="high")
    info = reg.lookup("k|1")
    assert (info.built_year, info.year_source) == (1977, "claude")


def test_clear_source_drops_only_that_tier(reg):
    reg.learn("a|1", built_year=2023, source="search", year_confidence="medium")
    reg.learn("b|1", built_year=1975, source="search", year_confidence="high")
    reg.learn("c|1", built_year=1988, source="quintoandar")
    assert reg.clear_source("search", max_confidence="medium") == 1
    assert reg.lookup("a|1").built_year is None
    assert reg.lookup("a|1").searched_at == ""
    assert reg.lookup("b|1").built_year == 1975
    assert reg.lookup("c|1").built_year == 1988


def test_a_year_of_unknown_provenance_is_only_an_estimate(isolated_registry):
    lst = _mk(condo_key="k|1", built_year=1999, condo_source="search")
    condos.resolve_condo(lst)
    assert lst.condo_confidence == "estimated"


def test_a_blocked_building_never_gets_a_year(tmp_path, monkeypatch):
    """A human 'I could not confirm this' has to outrank any resolver."""
    ov = tmp_path / "condo_overrides.csv"
    ov.write_text(
        "key,name,built_year,block\nrua humberto i|928,,,1\n", encoding="utf-8"
    )
    reg = CondoRegistry(path=tmp_path / "condos.json", overrides=ov)
    monkeypatch.setattr(condos, "_registry", reg)
    monkeypatch.setattr(condos.settings, "condo_search_enabled", True)
    monkeypatch.setattr(condos.settings, "condo_search_provider", "claude")
    condos.reset_run_budget()

    calls = []
    monkeypatch.setattr(
        "rental_finder.condo_agent.lookup_condo", lambda **kw: calls.append(kw)
    )
    lst = _mk(condo_key="rua humberto i|928")
    condos.resolve_condo(lst)
    assert calls == []  # not even looked up
    assert lst.built_year is None
    assert lst.condo_confidence == "not_found"
    assert reg.needs_search("rua humberto i|928") is False


def test_blocking_clears_a_year_already_stored(tmp_path):
    ov = tmp_path / "condo_overrides.csv"
    ov.write_text("key,name,built_year,block\nk|1,,,1\n", encoding="utf-8")
    cache = tmp_path / "condos.json"
    reg = CondoRegistry(path=cache, overrides=tmp_path / "none.csv")
    reg.learn("k|1", built_year=1978, source="claude", year_confidence="medium")
    reg2 = CondoRegistry(path=cache, overrides=ov)
    assert reg2.lookup("k|1").built_year is None
    assert reg2.lookup("k|1").blocked is True


def test_a_sibling_listing_waits_and_takes_the_answer(agent_registry, monkeypatch):
    """Ciomara has four rows in the base; the other three must not come back blank."""
    import threading

    started = threading.Event()

    def _slow(**kw):
        started.set()
        import time

        time.sleep(0.3)
        return _finding(built_year=1975, confidence="high", found_number="446")

    monkeypatch.setattr("rental_finder.condo_agent.lookup_condo", _slow)
    key = "rua eca de queiroz|446"
    winner = _mk(condo_key=key)
    sibling = _mk(condo_key=key, external_id="2")

    t = threading.Thread(target=condos.resolve_condo, args=(winner,))
    t.start()
    started.wait(2)
    condos.resolve_condo(sibling)
    t.join(5)

    assert winner.built_year == 1975
    assert sibling.built_year == 1975
    assert sibling.condo_confidence == "high"


def test_a_sibling_of_an_unresolved_building_is_marked_not_found(
    agent_registry, monkeypatch
):
    monkeypatch.setattr(
        "rental_finder.condo_agent.lookup_condo",
        lambda **kw: _finding(confidence="not_found"),
    )
    key = "rua alvorada|859"
    agent_registry.claim_search(key)          # someone else holds it
    agent_registry.release_search(key)        # ...and finished with nothing
    lst = _mk(condo_key=key)
    condos.resolve_condo(lst)
    assert lst.built_year is None


def test_clear_source_with_no_ceiling_clears_every_confidence(reg):
    reg.learn("a|1", built_year=2023, source="search", year_confidence="medium")
    reg.learn("b|1", built_year=1975, source="search", year_confidence="high")
    reg.learn("c|1", built_year=1988, source="quintoandar")
    assert reg.clear_source("search") == 2
    assert reg.lookup("a|1").built_year is None
    assert reg.lookup("b|1").built_year is None
    assert reg.lookup("c|1").built_year == 1988
