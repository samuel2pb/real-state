"""The agent is untrusted input: these are the gates its answer has to pass."""

from __future__ import annotations

import json
from datetime import date

import pytest

from rental_finder.condo_agent import (
    RESULT_SCHEMA,
    _loads_loose,
    build_prompt,
    validate,
)


def payload(**over) -> dict:
    base = {
        "built_year": 1977,
        "condo_name": "Villa Di Trevi",
        "confidence": "high",
        "source_url": "https://loft.com.br/condominio/villa-di-trevi",
        "found_street": "Rua Jose Antonio Coelho",
        "found_number": "603",
        "conflicting_years": [],
        "notes": "Loft ficha tecnica.",
    }
    base.update(over)
    return base


def test_clean_answer_survives():
    f = validate(
        payload(corroborating_urls=["https://www.lopes.com.br/condominios/x"]),
        number="603",
    )
    assert (f.built_year, f.confidence, f.name) == (1977, "high", "Villa Di Trevi")
    assert not f.rejected


def test_one_page_is_never_high():
    """Ed. Nilo and City Park both came back high off a single Loft ficha."""
    f = validate(payload(), number="603")
    assert (f.built_year, f.confidence) == (1977, "medium")


def test_two_pages_on_one_domain_are_not_two_sources():
    f = validate(
        payload(corroborating_urls=["https://loft.com.br/condominio/other-slug"]),
        number="603",
    )
    assert f.confidence == "medium"


def test_number_mismatch_is_rejected():
    """The Chibaras case: a real 1973 building, at 415, not at 381."""
    f = validate(payload(built_year=1973, found_number="415"), number="381")
    assert f.built_year is None
    assert f.confidence == "not_found"
    assert "415" in f.rejected


def test_number_suffix_still_matches():
    f = validate(payload(found_number="603-A"), number="603")
    assert f.built_year == 1977


def test_unreported_address_is_a_miss():
    """No address means nothing was verified — that is how 2023 got in."""
    f = validate(payload(found_number=None), number="603")
    assert f.built_year is None
    assert f.rejected == "source address not reported"


@pytest.mark.parametrize("year", [1899, 2099, date.today().year + 6])
def test_implausible_years_rejected(year):
    f = validate(payload(built_year=year), number="603")
    assert f.built_year is None
    assert "implausible" in f.rejected


def test_null_year_is_a_clean_miss():
    f = validate(payload(built_year=None, confidence="not_found"), number="603")
    assert (f.built_year, f.confidence, f.rejected) == (None, "not_found", "")


def test_divergent_records_resolve_to_the_oldest_year():
    """Loft keeps two pages for one building; a too-new year is the costly one.

    Live: avenida jandira|647 came back 1985 with [1983, 1985] seen, and
    alameda joaquim eugenio de lima|70 came back 1997 with [1996, 1997].
    """
    f = validate(
        payload(built_year=1985, conflicting_years=[1983, 1985], found_number="647"),
        number="647",
    )
    assert f.built_year == 1983
    assert f.confidence == "medium"
    assert f.conflicting_years == (1983, 1985)


def test_conflict_capping_survives_corroboration():
    f = validate(
        payload(
            built_year=1997,
            conflicting_years=[1996, 1997],
            corroborating_urls=["https://www.lopes.com.br/x"],
        ),
        number="603",
    )
    assert (f.built_year, f.confidence) == (1996, "medium")


def test_unknown_confidence_degrades_to_estimated():
    f = validate(payload(confidence="pretty sure"), number="603")
    assert f.confidence == "estimated"


def test_booleans_are_not_years():
    f = validate(payload(built_year=True), number="603")
    assert f.built_year is None


def test_loads_loose_handles_fences_and_prose():
    assert _loads_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert _loads_loose('here it is: {"a": 1} hope that helps') == {"a": 1}
    assert _loads_loose("no json at all") is None


def test_prompt_carries_address_and_hints():
    text = build_prompt(
        name="",
        street="Travessa Humberto I",
        number="962",
        neighborhood="Vila Mariana",
        hints={"condo_fee": 1300, "sqm": 90, "bedrooms": 3, "suites": 0},
    )
    assert "Travessa Humberto I" in text and "962" in text
    assert "1300" in text and "90" in text
    assert "Suites:" not in text  # a zero hint is noise, not a fact
    # no name is not a blocker, and the prompt must say so
    assert "no building name" in text


def test_schema_pins_the_confidence_vocabulary():
    assert RESULT_SCHEMA["properties"]["confidence"]["enum"] == [
        "high",
        "medium",
        "estimated",
        "not_found",
    ]


# --- subprocess wiring ----------------------------------------------------


class _Proc:
    def __init__(self, stdout, stderr=""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, 0


def test_run_agent_reads_the_cli_envelope(monkeypatch):
    from rental_finder import condo_agent

    seen = {}

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw["input"]
        return _Proc(
            json.dumps(
                {
                    "is_error": False,
                    "total_cost_usd": 0.08,
                    "result": json.dumps(payload()),
                }
            )
        )

    monkeypatch.setattr(condo_agent, "_cli", lambda: "claude")
    monkeypatch.setattr(condo_agent.subprocess, "run", _fake_run)
    out, cost, err = condo_agent.run_agent("prompt text")
    assert err == "" and cost == 0.08
    assert out["built_year"] == 1977
    assert seen["input"] == "prompt text"
    argv = seen["argv"]
    # the isolation flags are the point: a user hook must not rewrite our JSON
    assert "--safe-mode" in argv and "--json-schema" in argv
    assert argv[argv.index("--allowed-tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_agent_error_envelope_is_not_a_finding(monkeypatch):
    from rental_finder import condo_agent

    monkeypatch.setattr(condo_agent, "_cli", lambda: "claude")
    monkeypatch.setattr(
        condo_agent.subprocess,
        "run",
        lambda argv, **kw: _Proc(
            json.dumps({"is_error": True, "result": "401 OAuth token expired"})
        ),
    )
    out, _, err = condo_agent.run_agent("x")
    assert out is None and "401" in err


def test_lookup_returns_none_when_the_cli_is_missing(monkeypatch):
    from rental_finder import condo_agent

    monkeypatch.setattr(condo_agent, "_cli", lambda: None)
    assert (
        condo_agent.lookup_condo(name="", street="Rua Estela", number="755") is None
    )


def test_a_session_limit_stops_the_run_after_one_hit(monkeypatch):
    """47 calls were spawned after the cap; the second one should be free."""
    from rental_finder import condo_agent

    condo_agent.reset_rate_limit()
    calls = []

    def _fake_run(argv, **kw):
        calls.append(1)
        return _Proc(
            json.dumps(
                {
                    "is_error": True,
                    "result": "You've hit your session limit · resets 12:40pm",
                    "api_error_status": 429,
                }
            )
        )

    monkeypatch.setattr(condo_agent, "_cli", lambda: "claude")
    monkeypatch.setattr(condo_agent.subprocess, "run", _fake_run)

    first = condo_agent.lookup_condo(name="", street="Rua Estela", number="755")
    assert first is None
    assert condo_agent.rate_limited() is True

    second = condo_agent.lookup_condo(name="", street="Rua Gandavo", number="298")
    assert second is None
    assert len(calls) == 1  # the second never spawned a subprocess
    assert "session limit" in condo_agent.limit_reason()
    condo_agent.reset_rate_limit()


def test_an_ordinary_failure_does_not_trip_the_breaker(monkeypatch):
    from rental_finder import condo_agent

    condo_agent.reset_rate_limit()
    monkeypatch.setattr(condo_agent, "_cli", lambda: "claude")
    monkeypatch.setattr(
        condo_agent.subprocess,
        "run",
        lambda argv, **kw: _Proc(json.dumps({"is_error": True, "result": "timeout"})),
    )
    assert condo_agent.lookup_condo(name="", street="Rua Estela", number="755") is None
    assert condo_agent.rate_limited() is False
