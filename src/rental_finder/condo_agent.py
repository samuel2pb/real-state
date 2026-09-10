"""Resolve a building's name and construction year with `claude -p` + web tools.

Replaces the SERP-snippet tier (`condo_search.py`), which had two failure modes
that cost more than the coverage they bought:

* it wrote confident wrong years — `rua jose antonio coelho|603` had 1977's
  building recorded as 2023 off a neighbourhood *index* page that happened to
  contain the number and a street token, and that row then ranked first in the
  whole base;
* it could not identify a building at all when the listing carried no usable
  name, which is exactly the long tail that stayed empty.

An agent with WebSearch/WebFetch reads the ficha tecnica itself (Loft is the
only site that publishes "Construcao" consistently for older zona-sul
buildings) and can triangulate an unnamed building from condo fee + unit size.

Nothing the agent returns is trusted structurally: the year must be plausible
and the street number it reports must equal the number we asked about, or the
finding is downgraded to a miss here, not there.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import Template

import structlog

from .condos import first_number
from .config import settings

log = structlog.get_logger(__name__)

PROMPT_PATH = Path(__file__).with_name("prompts") / "condo_lookup.md"

MIN_YEAR = 1930
MAX_YEAR_AHEAD = 5

CONFIDENCES = ("high", "medium", "estimated", "not_found")
# Rank, so `condo_search_min_confidence` can gate what reaches Notion.
# "estimated" is recorded but never feeds BuiltYear by default: an estimate is
# exactly what the 2023 value looked like from the outside.
CONFIDENCE_RANK = {"not_found": 0, "estimated": 1, "medium": 2, "high": 3}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "built_year": {"type": ["integer", "null"]},
        "condo_name": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "source_url": {"type": ["string", "null"]},
        "found_street": {"type": ["string", "null"]},
        "found_number": {"type": ["string", "null"]},
        "conflicting_years": {"type": "array", "items": {"type": "integer"}},
        "corroborating_urls": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["built_year", "confidence", "notes"],
    "additionalProperties": True,
}


@dataclass(frozen=True)
class CondoFinding:
    """What the agent claims, after our own validation."""

    built_year: int | None = None
    name: str = ""
    confidence: str = "not_found"
    source_url: str = ""
    found_number: str = ""
    notes: str = ""
    conflicting_years: tuple[int, ...] = ()
    sources: tuple[str, ...] = ()
    cost_usd: float = 0.0
    rejected: str = ""  # why we dropped a year the agent did return

    @property
    def year(self) -> int | None:
        """Alias, so this drops into the old YearHit call sites."""
        return self.built_year

    @property
    def url(self) -> str:
        return self.source_url


# Once the subscription's session limit is hit, every further `claude -p` is a
# spawned process that comes straight back 429. Observed live: 47 consecutive
# failures after the cap, each costing a subprocess and several seconds. Trip a
# breaker on the first one and let the rest of the run fall through instantly.
_LIMIT_MARKERS = ("session limit", "usage limit", "rate limit", "429")
_breaker = threading.Event()
_breaker_reason = ""


def rate_limited() -> bool:
    """True once a run has hit the account's limit and should stop looking up."""
    return _breaker.is_set()


def limit_reason() -> str:
    return _breaker_reason


def reset_rate_limit() -> None:
    """Call at the start of a cycle; the limit resets on the account's clock."""
    global _breaker_reason
    _breaker.clear()
    _breaker_reason = ""


def _trip_breaker(err: str) -> bool:
    global _breaker_reason
    low = (err or "").lower()
    if not any(m in low for m in _LIMIT_MARKERS):
        return False
    if not _breaker.is_set():
        _breaker_reason = err[:200]
        log.error("condo_agent_rate_limited", err=_breaker_reason)
        _breaker.set()
    return True


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return (m.group(1) if m else "").lower()


def _plausible(year: int) -> bool:
    return MIN_YEAR <= year <= date.today().year + MAX_YEAR_AHEAD


def _hint_lines(hints: dict) -> str:
    """Extra facts that let the agent tell two condos on one street apart.

    The Luci e Lea building was identified by matching condo fee + unit size +
    bedroom count, not by name, so these are worth the tokens.
    """
    labels = {
        "condo_fee": "Monthly condo fee (R$)",
        "sqm": "Unit size (m2)",
        "bedrooms": "Bedrooms",
        "suites": "Suites",
        "parking": "Parking spots",
    }

    def _fmt(v):
        return int(v) if isinstance(v, float) and v.is_integer() else v

    rows = [
        f"- {labels[k]}: {_fmt(hints[k])}"
        for k in labels
        if hints.get(k) not in (None, "", 0, 0.0)
    ]
    if not rows:
        return ""
    head = (
        "\nExtra facts about one unit in this building (use them to tell "
        "candidate\nbuildings apart, not as evidence of the year):\n"
    )
    return head + "\n".join(rows)


def build_prompt(
    *,
    name: str,
    street: str,
    number: str,
    neighborhood: str,
    hints: dict | None = None,
) -> str:
    tmpl = Template(PROMPT_PATH.read_text("utf-8"))
    return tmpl.safe_substitute(
        street=street or "(unknown)",
        number=number or "(unknown)",
        neighborhood=neighborhood or "(unknown)",
        city=f"{settings.buy_city}, {settings.buy_state}, Brazil",
        name=name or "(the listing gives no building name)",
        hints=_hint_lines(hints or {}),
    )


def _cli() -> str | None:
    found = shutil.which(settings.condo_agent_cmd)
    if found:
        return found
    return settings.condo_agent_cmd if Path(settings.condo_agent_cmd).exists() else None


def _workdir() -> Path:
    """A stable scratch cwd, so agent sessions do not land in the repo history."""
    path = settings.cache_path / "condo_agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _argv(cli: str) -> list[str]:
    return [
        cli,
        "-p",
        # --safe-mode keeps the user's hooks, plugins, output styles, MCP
        # servers and CLAUDE.md out of this subprocess. A SessionStart hook that
        # rewrites the assistant's voice would happily rewrite our JSON too.
        "--safe-mode",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESULT_SCHEMA),
        "--model",
        settings.condo_agent_model,
        "--allowed-tools",
        "WebSearch,WebFetch",
        # dontAsk denies anything unlisted instead of blocking on a prompt that
        # nobody will answer in a scheduled run.
        "--permission-mode",
        "dontAsk",
        "--max-budget-usd",
        str(settings.condo_agent_max_usd),
    ]


def _loads_loose(text: str) -> dict | None:
    """The final message, whether it comes back bare or fenced."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


def run_agent(prompt: str) -> tuple[dict | None, float, str]:
    """(payload, cost usd, error). Never raises."""
    cli = _cli()
    if not cli:
        return None, 0.0, f"cli not found: {settings.condo_agent_cmd}"
    try:
        proc = subprocess.run(
            _argv(cli),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.condo_agent_timeout_sec,
            cwd=str(_workdir()),
        )
    except subprocess.TimeoutExpired:
        return None, 0.0, "timeout"
    except Exception as e:  # missing binary, permissions, ...
        return None, 0.0, str(e)[:200]

    try:
        envelope = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        err = (proc.stderr or proc.stdout or "").strip()[:200]
        return None, 0.0, err or "unparseable stdout"

    cost = float(envelope.get("total_cost_usd") or 0.0)
    if envelope.get("is_error"):
        # `result` is empty when the run was cut off rather than refused (budget
        # cap, turn limit), so carry the CLI's own reasons or the failure is
        # indistinguishable from a bad answer.
        why = " ".join(
            str(envelope.get(k))
            for k in ("terminal_reason", "stop_reason", "subtype", "api_error_status")
            if envelope.get(k)
        )
        return None, cost, (str(envelope.get("result") or "") + " " + why).strip()[:300]

    payload = envelope.get("result")
    if isinstance(payload, str):
        payload = _loads_loose(payload)
    if not isinstance(payload, dict):
        return None, cost, "result was not a JSON object"
    return payload, cost, ""


def validate(payload: dict, *, number: str, cost_usd: float = 0.0) -> CondoFinding:
    """Turn the agent's claim into a finding we are willing to store.

    The number gate is the rule the SERP tier never had: the Chibaras search
    found a real "Edificio Chibaras" built in 1973 — at number 415, not 381.
    Different number, different building, no year.
    """
    conf = str(payload.get("confidence") or "").strip().lower()
    if conf not in CONFIDENCES:
        conf = "estimated"
    name = str(payload.get("condo_name") or "").strip()
    url = str(payload.get("source_url") or "").strip()
    notes = str(payload.get("notes") or "").strip()[:500]
    found_number = first_number(payload.get("found_number"))
    conflicts = tuple(
        sorted(
            {
                int(y)
                for y in (payload.get("conflicting_years") or [])
                if isinstance(y, int) and not isinstance(y, bool) and _plausible(y)
            }
        )
    )

    urls = [url] + [
        str(u).strip()
        for u in (payload.get("corroborating_urls") or [])
        if str(u or "").strip()
    ]
    domains = {_domain(u) for u in urls if _domain(u)}

    raw_year = payload.get("built_year")
    year = (
        int(raw_year) if isinstance(raw_year, int) and not isinstance(raw_year, bool) else None
    )
    rejected = ""
    want = first_number(number)

    if year is not None and not _plausible(year):
        year, rejected = None, f"implausible year {raw_year}"
    elif year is not None and want and not found_number:
        # The agent did not say which address it read. That is the hole the 2023
        # value came through, so it is a miss, not a medium.
        year, rejected = None, "source address not reported"
    elif year is not None and want and found_number != want:
        year, rejected = (
            None,
            f"number mismatch: source says {found_number}, key says {want}",
        )

    if year is not None and len(conflicts) > 1:
        # Loft keeps duplicate records for one building whose years disagree by a
        # year or two. Pick in our code, not in the agent's judgement, and pick
        # the oldest: the expensive mistake in this base is a building looking
        # newer than it is, which is what floated a 1977 build to the top.
        oldest = min(conflicts)
        if oldest != year:
            log.info(
                "condo_year_conflict", picked=oldest, over=year, seen=list(conflicts)
            )
            year = oldest
        conf = "medium" if conf == "high" else conf

    if year is None:
        conf = "not_found"
    elif conf == "high" and len(domains) < 2:
        # One page is never "high". The Ed. Nilo and City Park answers both came
        # back high off a single Loft ficha, and both are buildings a human
        # refused to commit to.
        conf = "medium"

    return CondoFinding(
        built_year=year,
        name=name,
        confidence=conf,
        source_url=url,
        found_number=found_number,
        notes=notes,
        conflicting_years=conflicts,
        sources=tuple(dict.fromkeys(urls)),
        cost_usd=cost_usd,
        rejected=rejected,
    )


def lookup_condo(
    *,
    name: str,
    street: str,
    number: str,
    neighborhood: str = "",
    hints: dict | None = None,
) -> CondoFinding | None:
    """One agent call per building. None on a transport/agent failure.

    A finding with `built_year is None` is a *result* — the building was looked
    at and no year is published — and gets parked by the registry. `None` means
    we never got an answer, and that is worth retrying.
    """
    number = first_number(number)
    if not street or not number:
        return None
    prompt = build_prompt(
        name=name, street=street, number=number, neighborhood=neighborhood, hints=hints
    )
    if rate_limited():
        return None
    payload, cost, err = run_agent(prompt)
    if payload is None:
        if not _trip_breaker(err):
            log.warning("condo_agent_err", street=street, number=number, err=err)
        return None
    finding = validate(payload, number=number, cost_usd=cost)
    log.info(
        "condo_agent",
        street=street,
        number=number,
        name=name or None,
        year=finding.built_year,
        confidence=finding.confidence,
        found_name=finding.name or None,
        url=finding.source_url or None,
        conflicts=list(finding.conflicting_years) or None,
        rejected=finding.rejected or None,
        cost_usd=round(cost, 4),
    )
    return finding
