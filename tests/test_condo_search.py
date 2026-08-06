"""Extraction + scoring, driven by the real SERP phrasings seen while probing."""

from datetime import date

import pytest

from rental_finder import condo_search
from rental_finder.condo_search import _score, build_query, extract_year

YEAR = date.today().year


def _r(url, title, snippet):
    return {"link": url, "title": title, "snippet": snippet}


# --- extraction -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("O condomínio foi construído em 1982.", (1982, "high")),
        ("Ano de construção: 1995", (1995, "high")),
        ("Construção de 1995", (1995, "high")),
        ("Park Lane Ibirapuera foi entregue em julho de 2023", (2023, "high")),
        (
            "Construído pela renomada incorporadora EZTEC em 2005, o Mustique "
            "Brooklin possui arquitetura neoclássica",
            (2005, "high"),
        ),
        ("Construído em 1995 (há 29 anos)", (1995, "high")),
        ("Prédio entregue há 29 anos", (YEAR - 29, "medium")),
        ("7 imóveis à venda no Le Grand Palais", None),
        ("empresa fundada em 21/01/1985", None),
    ],
)
def test_extract_year(text, expected):
    assert extract_year(text) == expected


@pytest.mark.parametrize("year", [1899, 1929, YEAR + 6, 2100])
def test_implausible_years_rejected(year):
    assert extract_year(f"construído em {year}") is None


# --- query ----------------------------------------------------------------


def test_query_quotes_name_and_address():
    q = build_query("Park Lane", "Rua Ministro Gabriel de Rezende Passos", "319", "Moema")
    assert '"Park Lane"' in q
    assert '"Rua Ministro Gabriel de Rezende Passos, 319"' in q
    assert "Moema" in q
    assert "ano de construção" in q


# --- scoring guards -------------------------------------------------------


def test_two_domains_agreeing_is_high_confidence():
    hit = _score(
        [
            _r("https://loft.com.br/condominio/x", "Mustique - Rua Arandu, 222", "construído em 2005"),
            _r("https://www.imovelweb.com.br/condominio/y", "Mustique Rua Arandu 222", "Ano de construção: 2005"),
        ],
        "Rua Arandu",
        "222",
    )
    assert (hit.year, hit.confidence) == (2005, "high")


def test_single_domain_is_medium_confidence():
    hit = _score(
        [_r("https://loft.com.br/c/x", "Rousseau - Alameda Jauaperi, 1120", "construído em 1982")],
        "Alameda Jauaperi",
        "1120",
    )
    assert (hit.year, hit.confidence) == (1982, "medium")


def test_company_registry_snippet_rejected():
    # diariocidade publishes a CNPJ founding date, not a building age
    hit = _score(
        [
            _r(
                "https://www.diariocidade.com/sp/sao-paulo/guia/condominio-edificio-rousseau",
                "Condominio Edificio Rousseau, Alameda Jauaperi 1120",
                "empresa fundada em 21/01/1985",
            )
        ],
        "Alameda Jauaperi",
        "1120",
    )
    assert hit is None


def test_wrong_address_rejected():
    # right name, another building entirely
    hit = _score(
        [_r("https://loft.com.br/c/x", "Condomínio Park Lane - Rua Qualquer, 900", "construído em 1999")],
        "Rua Ministro Gabriel de Rezende Passos",
        "319",
    )
    assert hit is None


def test_unlisted_domain_ignored():
    hit = _score(
        [_r("https://algum-blog.com/post", "Mustique Rua Arandu, 222", "construído em 2005")],
        "Rua Arandu",
        "222",
    )
    assert hit is None


def test_conflicting_years_take_modal_and_drop_to_medium():
    hit = _score(
        [
            _r("https://loft.com.br/a", "Mustique Rua Arandu, 222", "construído em 2005"),
            _r("https://www.imovelweb.com.br/b", "Mustique Rua Arandu 222", "construído em 2005"),
            _r("https://www.lopes.com.br/c", "Mustique Rua Arandu 222", "construído em 1998"),
        ],
        "Rua Arandu",
        "222",
    )
    assert (hit.year, hit.confidence) == (2005, "medium")


def test_no_results_is_none():
    assert _score([], "Rua Arandu", "222") is None


# --- provider plumbing ----------------------------------------------------


def test_search_year_without_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(condo_search.settings, "condo_search_api_key", "")
    assert condo_search.search_year(name="X", street="Rua Y", number="1") is None


def test_search_year_end_to_end(monkeypatch):
    monkeypatch.setattr(condo_search.settings, "condo_search_api_key", "k")
    monkeypatch.setattr(condo_search.settings, "condo_search_provider", "serper")
    monkeypatch.setattr(condo_search.settings, "condo_search_min_confidence", "medium")
    monkeypatch.setitem(
        condo_search.PROVIDERS,
        "serper",
        lambda q: [
            _r("https://loft.com.br/c/x", "Rousseau - Alameda Jauaperi, 1120", "construído em 1982")
        ],
    )
    hit = condo_search.search_year(
        name="Rousseau", street="Alameda Jauaperi", number="1120", neighborhood="Moema"
    )
    assert (hit.year, hit.domain) == (1982, "loft.com.br")


def test_min_confidence_high_drops_single_domain(monkeypatch):
    monkeypatch.setattr(condo_search.settings, "condo_search_api_key", "k")
    monkeypatch.setattr(condo_search.settings, "condo_search_min_confidence", "high")
    monkeypatch.setitem(
        condo_search.PROVIDERS,
        "serper",
        lambda q: [
            _r("https://loft.com.br/c/x", "Rousseau - Alameda Jauaperi, 1120", "construído em 1982")
        ],
    )
    assert (
        condo_search.search_year(
            name="Rousseau", street="Alameda Jauaperi", number="1120"
        )
        is None
    )


def test_provider_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(condo_search.settings, "condo_search_api_key", "k")

    def _boom(q):
        raise RuntimeError("429")

    monkeypatch.setitem(condo_search.PROVIDERS, "serper", _boom)
    assert condo_search.search_year(name="X", street="Rua Y", number="1") is None
