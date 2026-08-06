"""Resolve a building's construction year from web-search snippets.

The year is read from SERP titles/snippets only — the sites that carry a ficha
técnica (loft, imovelweb, 123i, casamineira) all return 403 to a direct fetch,
and the snippet already holds the answer. Never fetch the result pages.

One query per *building*, cached forever by CondoRegistry, so the quota cost of
this module is a few hundred queries total.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import structlog

from .config import settings
from .condos import first_number, strip_accents
from .http_client import CurlSession

log = structlog.get_logger(__name__)

# Domains known to publish a building's ficha técnica.
YEAR_DOMAINS = {
    "loft.com.br",
    "imovelweb.com.br",
    "vivareal.com.br",
    "zapimoveis.com.br",
    "123i.com.br",
    "casamineira.com.br",
    "lopes.com.br",
    "npiconsultoria.com.br",
    "imovelguide.com.br",
    "agenteimovel.com.br",
    "b4k.com.br",
    "chavesnamao.com.br",
    "homesphere.com.br",
    "finderimoveis.com.br",
    "ronniesimoes.com.br",
}

# Company registries: they publish a CNPJ founding date, not a building age.
BLOCKED_DOMAINS = {
    "diariocidade.com",
    "econodata.com.br",
    "cnpj.biz",
    "cnpja.com",
    "casadosdados.com.br",
    "empresascnpj.com",
    "consultacnpj.com",
}

MIN_YEAR = 1930
MAX_YEAR_AHEAD = 5

_EXPLICIT_YEAR_RE = re.compile(
    r"(?:constru[íi]d[oa]|entregue|inaugurad[oa]|lan[çc]ad[oa])"
    r"(?:\s+(?:pel[ao]\s+\S+|em|no ano de|na d[ée]cada de))?"
    r"[^.,;]{0,40}?\b((?:19|20)\d{2})\b"
    r"|ano\s+de\s+constru[çc][ãa]o\s*[:\-]?\s*\b((?:19|20)\d{2})\b"
    r"|constru[çc][ãa]o\s+de\s+\b((?:19|20)\d{2})\b",
    re.I,
)
_RELATIVE_AGE_RE = re.compile(r"h[áa]\s+(\d{1,3})\s+anos", re.I)


@dataclass(frozen=True)
class YearHit:
    year: int
    confidence: str  # "high" | "medium"
    url: str
    domain: str


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return (m.group(1) if m else "").lower()


def _plausible(year: int) -> bool:
    return MIN_YEAR <= year <= date.today().year + MAX_YEAR_AHEAD


def build_query(name: str, street: str, number: str, neighborhood: str) -> str:
    """Quote the name AND the street+number pair — a bare name matches anywhere."""
    parts = [f'condomínio "{name}"']
    if street and number:
        parts.append(f'"{street}, {number}"')
    elif street:
        parts.append(f'"{street}"')
    if neighborhood:
        parts.append(neighborhood)
    parts.append(f"{settings.buy_city} ano de construção")
    return " ".join(parts)


def _corroborates(text: str, street: str, number: str) -> bool:
    """The snippet must name this address, or it is about another building."""
    if not number:
        return False
    flat = strip_accents(text).lower()
    if not re.search(rf"\b{re.escape(number)}\b", flat):
        return False
    tokens = [
        t
        for t in strip_accents(street).lower().split()
        if len(t) > 3 and t not in {"rua", "avenida", "alameda", "praca", "travessa"}
    ]
    return any(t in flat for t in tokens) if tokens else False


def extract_year(text: str) -> tuple[int, str] | None:
    """(year, confidence) from one snippet. Explicit year beats a relative age."""
    for m in _EXPLICIT_YEAR_RE.finditer(text or ""):
        raw = next((g for g in m.groups() if g), None)
        if raw and _plausible(int(raw)):
            return int(raw), "high"
    m = _RELATIVE_AGE_RE.search(text or "")
    if m:
        year = date.today().year - int(m.group(1))
        if _plausible(year):
            return year, "medium"
    return None


def _score(results: list[dict], street: str, number: str) -> YearHit | None:
    """Vote across whitelisted domains; agreement between two of them wins."""
    votes: dict[int, list[tuple[str, str, str]]] = {}
    for r in results:
        url = r.get("link") or r.get("url") or ""
        dom = _domain(url)
        if not dom or dom in BLOCKED_DOMAINS:
            continue
        if dom not in YEAR_DOMAINS:
            continue
        text = " ".join(
            filter(None, [r.get("title"), r.get("snippet") or r.get("description")])
        )
        if not _corroborates(text, street, number):
            continue
        found = extract_year(text)
        if not found:
            continue
        year, conf = found
        votes.setdefault(year, []).append((dom, url, conf))

    if not votes:
        return None
    # modal year; ties broken by the count of distinct domains, then by recency
    best_year = max(
        votes,
        key=lambda y: (len({d for d, _, _ in votes[y]}), len(votes[y])),
    )
    entries = votes[best_year]
    domains = {d for d, _, _ in entries}
    conflicting = len(votes) > 1
    explicit = all(c == "high" for _, _, c in entries)
    confidence = "high" if (len(domains) >= 2 and explicit and not conflicting) else "medium"
    if conflicting:
        log.info(
            "condo_year_conflict",
            years=sorted(votes),
            picked=best_year,
            domains=sorted(domains),
        )
    dom, url, _ = entries[0]
    return YearHit(year=best_year, confidence=confidence, url=url, domain=dom)


# --- providers -------------------------------------------------------------

_session: CurlSession | None = None


def _http() -> CurlSession:
    global _session
    if _session is None:
        _session = CurlSession("condo_search")
    return _session


def _serper(query: str) -> list[dict]:
    r = _http().post(
        "https://google.serper.dev/search",
        json={"q": query, "gl": "br", "hl": "pt-br", "num": 10},
        headers={
            "X-API-KEY": settings.condo_search_api_key,
            "Content-Type": "application/json",
        },
    )
    return (r.json() or {}).get("organic") or []


def _brave(query: str) -> list[dict]:
    r = _http().get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "country": "br", "search_lang": "pt", "count": 10},
        headers={
            "X-Subscription-Token": settings.condo_search_api_key,
            "Accept": "application/json",
        },
    )
    return ((r.json() or {}).get("web") or {}).get("results") or []


def _google_cse(query: str) -> list[dict]:
    r = _http().get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": settings.condo_search_api_key,
            "cx": settings.condo_search_cse_id,
            "q": query,
            "gl": "br",
            "hl": "pt-BR",
            "num": 10,
        },
    )
    return (r.json() or {}).get("items") or []


PROVIDERS = {"serper": _serper, "brave": _brave, "google_cse": _google_cse}


def search_year(
    *, name: str, street: str, number: str, neighborhood: str = ""
) -> YearHit | None:
    """One search per building. Returns None on miss, low confidence, or error."""
    if not name or not settings.condo_search_api_key:
        if not settings.condo_search_api_key:
            log.warning("condo_search_no_key", provider=settings.condo_search_provider)
        return None
    number = first_number(number)
    query = build_query(name, street, number, neighborhood)
    provider = PROVIDERS.get(settings.condo_search_provider)
    if provider is None:
        log.error("condo_search_bad_provider", provider=settings.condo_search_provider)
        return None
    try:
        results = provider(query)
    except Exception as e:
        log.warning("condo_search_err", name=name, err=str(e)[:200])
        return None

    hit = _score(results, street, number)
    if hit and settings.condo_search_min_confidence == "high" and hit.confidence != "high":
        log.info("condo_search_low_conf", name=name, year=hit.year, conf=hit.confidence)
        hit = None
    log.info(
        "condo_search",
        name=name,
        street=street,
        number=number,
        results=len(results),
        year=hit.year if hit else None,
        confidence=hit.confidence if hit else None,
        domain=hit.domain if hit else None,
    )
    return hit
