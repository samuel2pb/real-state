"""Condominium identity, registry and enrichment.

A building is identified by CEP + street number ("04524020|191"). QuintoAndar
serves both on every listing, and it is a far stronger join key than a
normalized street name. Everything resolved for a building is cached in
`.cache/condos.json` so a name/year is looked up once, not once per listing.
"""

from __future__ import annotations

import csv
import json
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import structlog

from .config import ROOT, settings
from .models import Listing

log = structlog.get_logger(__name__)

# Higher wins. The agent outranks QuintoAndar for the *year*: QA serves a year
# on ~10% of listings and partner classifieds carry whatever the partner typed,
# while the agent reads a ficha técnica and reports the address it read it from.
# It stays below `manual`. `search` is the old SERP-snippet tier, demoted below
# everything because it is the tier that wrote 2023 on a 1977 building.
SOURCE_RANK = {"": 0, "search": 1, "tpcl": 2, "quintoandar": 3, "claude": 4, "manual": 5}

# Names are the other way round: QuintoAndar's `condominium.name` is the
# building's own record, so it outranks a name the agent read off a broker page.
# The agent's name still lands on every building QA left unnamed.
NAME_RANK = {"": 0, "search": 1, "claude": 2, "quintoandar": 3, "manual": 4}

_STREET_TYPES = {
    "av": "avenida",
    "av.": "avenida",
    "avenida": "avenida",
    "r": "rua",
    "r.": "rua",
    "rua": "rua",
    "al": "alameda",
    "al.": "alameda",
    "alameda": "alameda",
    "pc": "praca",
    "pca": "praca",
    "praca": "praca",
    "tv": "travessa",
    "travessa": "travessa",
    "estr": "estrada",
    "estrada": "estrada",
}

_NAME_PREFIX_RE = re.compile(r"^(?:do|da|de|dos|das)\s+", re.I)
_NAME_SUFFIX_RE = re.compile(r"\s*[-–]\s*\(?[^()]{2,40}?\)?\s*$")
_BUILDING_WORD_RE = re.compile(
    r"^(?:ed\.?|edif[ií]cio|cond\.?|condom[ií]nio|residencial)\s+", re.I
)
_TRAILING_NUM_RE = re.compile(r"^(.*?)[,\s]+(\d[\dA-Za-z\-]*)\s*$")
_LOWER_WORDS = {"de", "da", "do", "das", "dos", "e", "em", "no", "na"}


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return nfkd.encode("ascii", "ignore").decode("ascii")


def _digits(value: str | int | None) -> str:
    return re.sub(r"\D", "", str(value or ""))


def first_number(value: str | int | None) -> str:
    """'191-A' -> '191'; '' when there is no leading digit run."""
    m = re.match(r"\s*(\d+)", str(value or ""))
    return m.group(1) if m else ""


def split_street_number(street: str, number: str | int | None) -> tuple[str, str]:
    """QuintoAndar sometimes packs the number into the street.

    ('Avenida Agami 333', None) -> ('Avenida Agami', '333')
    """
    num = first_number(number)
    street = (street or "").strip()
    if num:
        return street, num
    m = _TRAILING_NUM_RE.match(street)
    if m:
        return m.group(1).strip(), first_number(m.group(2))
    return street, ""


def condo_key(street: str, number: str | int | None) -> str:
    """Building identity: normalized street + number, e.g. 'avenida ibijau|191'.

    Street rather than CEP: QuintoAndar's /classificado listings (the majority of
    the buy result set) serve no zipCode at all, while street lands on 100% of
    both routes. The CEP is kept as registry metadata for later address joins.
    """
    street, num = split_street_number(street, number)
    tokens = strip_accents(street).lower().split()
    if not tokens or not num:
        return ""
    if tokens[0] in _STREET_TYPES:
        tokens[0] = _STREET_TYPES[tokens[0]]
    norm = " ".join(t for t in tokens if t)
    return f"{norm}|{num}" if norm else ""


def split_key(key: str) -> tuple[str, str]:
    """'rua jose antonio coelho|603' -> ('rua jose antonio coelho', '603').

    The street comes back normalized (lowercase, unaccented), which is fine for
    a web search and is the only address a Notion backfill row carries.
    """
    street, _, number = (key or "").partition("|")
    return street.strip(), first_number(number)


def normalize_cep(zip_code: str) -> str:
    """8-digit CEP, or empty. Metadata only — never the key."""
    cep = _digits(zip_code)
    return cep if len(cep) == 8 else ""


def clean_condo_name(raw: str) -> str:
    """Normalize the messy names QuintoAndar serves.

    'do Edifício Twins Towers - (Moema, SP)' -> 'Edifício Twins Towers'
    'ILHA DE CRETA - Campo Belo'             -> 'Ilha de Creta'
    """
    name = (raw or "").strip()
    if not name:
        return ""
    name = _NAME_PREFIX_RE.sub("", name).strip()
    name = _NAME_SUFFIX_RE.sub("", name).strip()
    if name.isupper():
        name = " ".join(
            w.lower() if w.lower() in _LOWER_WORDS else w.capitalize()
            for w in name.split()
        )
        # never lowercase the first word
        parts = name.split()
        if parts:
            parts[0] = parts[0].capitalize()
            name = " ".join(parts)
    return name.strip(" -–,")


def looks_like_name(name: str) -> bool:
    """Reject values that are not a building name at all.

    QuintoAndar's condominium.name is free text and sometimes carries a bare
    number ('8307' at Rua Bacaetava 181). Storing that as a condo name is worse
    than storing nothing, and searching a year for it is a wasted credit.
    """
    cleaned = re.sub(r"[^0-9a-zA-ZÀ-ÿ]", "", name or "")
    return bool(cleaned) and not cleaned.isdigit() and len(cleaned) >= 3


def search_name(name: str) -> str:
    """Name without the generic building word, for use in a search query."""
    return _BUILDING_WORD_RE.sub("", clean_condo_name(name)).strip() or name


@dataclass
class CondoInfo:
    key: str
    name: str = ""
    name_raw: str = ""
    built_year: int | None = None
    year_source: str = ""
    year_confidence: str = ""
    year_source_url: str = ""
    # Why the agent answered what it answered, and every year it saw when the
    # sources disagreed. Kept so a wrong value is traceable without re-running.
    year_notes: str = ""
    year_conflicts: str = ""
    # Set by an override row with block=1: this building's year is disputed and
    # no resolver may write one. The point of the whole tier is not writing a
    # year nobody has confirmed, so a human "I could not confirm this" has to be
    # expressible — otherwise the only way to overrule the agent is to invent a
    # year, which is the bug.
    blocked: bool = False
    name_source: str = ""
    street: str = ""
    number: str = ""
    cep: str = ""
    qa_hash: str = ""
    searched_at: str = ""
    updated: str = field(default_factory=lambda: date.today().isoformat())


class CondoRegistry:
    """Thread-safe JSON-backed store of everything known per building."""

    def __init__(self, path: Path | None = None, overrides: Path | None = None):
        self.path = path or (settings.cache_path / settings.condo_cache_file)
        self._lock = threading.Lock()
        self._data: dict[str, CondoInfo] = {}
        # key -> event, set when whoever claimed the key finishes. A sibling
        # listing in the same building waits on it instead of walking away with
        # nothing, which is how 19 rows across 9 buildings ended up with no
        # CondoConfidence on the first backfill.
        self._searching: dict[str, threading.Event] = {}
        self._load()
        self._load_overrides(
            overrides or (ROOT / settings.condo_overrides_file)
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception as e:
            log.warning("condo_cache_unreadable", path=str(self.path), err=str(e))
            return
        for key, row in raw.items():
            row.pop("key", None)
            try:
                self._data[key] = CondoInfo(key=key, **row)
            except TypeError:
                # tolerate a cache written by an older/newer field set
                known = CondoInfo.__dataclass_fields__
                self._data[key] = CondoInfo(
                    key=key, **{k: v for k, v in row.items() if k in known}
                )

    def _load_overrides(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("key") or "").strip()
                if not key:
                    continue
                year = first_number(row.get("built_year"))
                blocked = (row.get("block") or "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "y",
                )
                self.learn(
                    key,
                    name=(row.get("name") or "").strip() or None,
                    built_year=int(year) if year else None,
                    source="manual",
                    year_confidence="high",
                    persist=False,
                    blocked=blocked or None,
                )
        log.info("condo_overrides_loaded", path=str(path), total=len(self._data))

    def _flush(self) -> None:
        """Caller must hold the lock."""
        payload = {}
        for key, info in self._data.items():
            row = asdict(info)
            row.pop("key")
            payload[key] = row
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    def lookup(self, key: str) -> CondoInfo | None:
        if not key:
            return None
        with self._lock:
            info = self._data.get(key)
            return CondoInfo(**asdict(info)) if info else None

    def learn(
        self,
        key: str,
        *,
        name: str | None = None,
        built_year: int | None = None,
        source: str,
        persist: bool = True,
        **meta,
    ) -> None:
        """Merge what we just learned. Never downgrades, never nulls a value."""
        if not key:
            return
        rank = SOURCE_RANK.get(source, 0)
        name_rank = NAME_RANK.get(source, 0)
        with self._lock:
            info = self._data.setdefault(key, CondoInfo(key=key))
            if name and not looks_like_name(name):
                name = None
            if name and name_rank >= NAME_RANK.get(info.name_source, 0):
                info.name_raw = meta.get("name_raw") or name
                info.name = clean_condo_name(name)
                info.name_source = source
            if built_year and rank >= SOURCE_RANK.get(info.year_source, 0):
                info.built_year = built_year
                info.year_source = source
                info.year_confidence = meta.get("year_confidence") or ""
                info.year_source_url = meta.get("year_source_url") or ""
            if meta.get("blocked"):
                info.blocked = True
                info.built_year = None
                info.year_source = ""
                info.year_confidence = ""
            for attr in (
                "street",
                "number",
                "cep",
                "qa_hash",
                "searched_at",
                "year_notes",
                "year_conflicts",
            ):
                val = meta.get(attr)
                if val:
                    setattr(info, attr, str(val))
            info.updated = date.today().isoformat()
            if persist:
                self._flush()

    def needs_search(self, key: str) -> bool:
        """True only for a building we have never searched and have no year for.

        A previous miss is parked permanently by default: the year is not in any
        snippet for that building, so retrying it just burns a credit on every
        run. `condo_search_retry_misses` (or clear_misses()) reopens them.
        """
        info = self.lookup(key)
        if info is None:
            return True
        if info.blocked or info.built_year:
            return False
        if not info.searched_at:
            return True
        if not settings.condo_search_retry_misses:
            return False
        try:
            last = date.fromisoformat(info.searched_at)
        except ValueError:
            return True
        return date.today() - last >= timedelta(days=settings.condo_search_retry_days)

    def clear_source(self, source: str, *, max_confidence: str = "") -> int:
        """Drop every year that came from `source`, so it is resolved again.

        The SERP tier's 328 `medium` years were all scored the same way as the
        one confirmed wrong value (`rua jose antonio coelho|603` → 2023, taken
        off a neighbourhood index page). Clearing them is the only way to stop
        trusting a number nobody ever checked.
        """
        order = {"estimated": 1, "medium": 2, "high": 3}
        # No ceiling given means "every confidence", not "only the blank ones" —
        # an empty string must not fall through to rank 0.
        ceiling = order.get(max_confidence, 99) if max_confidence else 99
        with self._lock:
            hit = [
                info
                for info in self._data.values()
                if info.built_year
                and info.year_source == source
                and order.get(info.year_confidence, 0) <= ceiling
            ]
            for info in hit:
                info.built_year = None
                info.year_source = ""
                info.year_confidence = ""
                info.year_source_url = ""
                info.year_notes = ""
                info.year_conflicts = ""
                info.searched_at = ""
            if hit:
                self._flush()
        return len(hit)

    def clear_misses(self) -> int:
        """Reopen every parked miss for searching. Returns how many."""
        with self._lock:
            reopened = [
                info
                for info in self._data.values()
                if info.searched_at and not info.built_year
            ]
            for info in reopened:
                info.searched_at = ""
            if reopened:
                self._flush()
        return len(reopened)

    def claim_search(self, key: str) -> bool:
        """Atomically reserve the right to search this building.

        Without this, two listings in the same building both pass needs_search()
        before either writes and the query is paid for twice — observed live on
        'Isola di Palamos' and 'San Juan San Justin'.
        """
        if not key:
            return False
        with self._lock:
            if key in self._searching:
                return False
            self._searching[key] = threading.Event()
        if not self.needs_search(key):
            self.release_search(key)
            return False
        return True

    def release_search(self, key: str) -> None:
        with self._lock:
            event = self._searching.pop(key, None)
        if event is not None:
            event.set()

    def wait_for_search(self, key: str, timeout: float) -> bool:
        """Block until whoever claimed `key` is done. True if it finished."""
        with self._lock:
            event = self._searching.get(key)
        return True if event is None else event.wait(timeout)

    def stats(self) -> dict:
        with self._lock:
            rows = list(self._data.values())
        by_source: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        for r in rows:
            if r.built_year:
                by_source[r.year_source or "?"] = by_source.get(r.year_source or "?", 0) + 1
                key = r.year_confidence or "?"
                by_conf[key] = by_conf.get(key, 0) + 1
        return {
            "buildings": len(rows),
            "with_name": sum(1 for r in rows if r.name),
            "with_year": sum(1 for r in rows if r.built_year),
            # parked: searched once, no year found, will not be queried again
            "parked_misses": sum(
                1 for r in rows if r.searched_at and not r.built_year
            ),
            "never_searched": sum(
                1 for r in rows if not r.searched_at and not r.built_year
            ),
            "by_year_source": by_source,
            "by_year_confidence": by_conf,
        }


_registry: CondoRegistry | None = None
_registry_lock = threading.Lock()
# One SERP call at a time (their rate limits); a couple of `claude -p`
# subprocesses in parallel, since each one is minutes of web reading.
_search_budget = threading.Semaphore(
    max(1, settings.condo_agent_concurrency)
    if settings.condo_search_provider == "claude"
    else 1
)
_run_searches = 0
_run_lock = threading.Lock()


def registry() -> CondoRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = CondoRegistry()
        return _registry


def reset_run_budget() -> None:
    """Call once at the start of a cycle; caps search spend per run."""
    global _run_searches
    with _run_lock:
        _run_searches = 0


def _take_search_slot() -> bool:
    global _run_searches
    with _run_lock:
        if _run_searches >= settings.condo_search_max_per_run:
            return False
        _run_searches += 1
        return True


def is_apartment(lst: Listing) -> bool:
    return (lst.property_type or "").strip().lower() in (
        settings.condo_apartment_types_list
    )


_CONF_ORDER = {"": 0, "not_found": 0, "estimated": 1, "medium": 2, "high": 3}


def _gate_on() -> bool:
    s = settings
    return s.condo_age_filter_enabled and s.condo_max_age_years > 0


def year_trusted(year: int | None, confidence: str = "") -> bool:
    """A year good enough to decide on: present and at the confidence floor."""
    floor = _CONF_ORDER.get(settings.condo_age_min_confidence, 2)
    return bool(year) and _CONF_ORDER.get(confidence, 0) >= floor


def year_too_old(year: int | None, confidence: str = "") -> bool:
    """Is a building of this year provably past the age cap?

    Only a trusted year can prove it. Whether an *unknown* year is acceptable
    is a separate question — see age_verdict.
    """
    if not _gate_on() or not year_trusted(year, confidence):
        return False
    return (date.today().year - int(year)) > settings.condo_max_age_years


def age_verdict(year: int | None, confidence: str = "") -> str:
    """'' keeps the listing; 'age' = too old; 'age_unknown' = no trusted year.

    An uncorroborated year counts as unknown, not as young: with
    condo_age_require_known on, it is rejected like a missing one.
    """
    if not _gate_on():
        return ""
    if year_too_old(year, confidence):
        return "age"
    if settings.condo_age_require_known and not year_trusted(year, confidence):
        return "age_unknown"
    return ""


def listing_age_verdict(lst: Listing) -> str:
    return age_verdict(lst.built_year, lst.condo_confidence)


def registry_age_verdict(key: str) -> str:
    """Age verdict from the cache alone — no network, no agent, no cost.

    This is what makes the gate cheap on every run after the first: a building
    resolved once is decided for free for every listing in it, forever. Only
    a *final* answer rejects here. A building nobody has searched yet goes on
    to the detail fetch and the resolver, and is decided after that.
    """
    if not key or not _gate_on():
        return ""
    info = registry().lookup(key)
    if info is None:
        return ""
    if info.blocked:
        # Nobody may state a year for it, so its age can never become known.
        return "age_unknown" if settings.condo_age_require_known else ""
    verdict = age_verdict(info.built_year, info.year_confidence)
    if verdict == "age_unknown" and registry().needs_search(key):
        return ""  # still resolvable: let this run try
    return verdict


def _apply_registry(lst: Listing, info: CondoInfo | None) -> None:
    """Copy what the registry knows about this building onto the listing."""
    if not info:
        return
    if info.name:
        lst.condo_name = info.name
        lst.condo_name_raw = lst.condo_name_raw or info.name_raw
    if info.built_year and not lst.built_year:
        lst.apply_built_year(info.built_year)
        lst.condo_source = info.year_source
        lst.condo_confidence = info.year_confidence
    elif info.built_year and not lst.condo_source:
        lst.condo_source = info.year_source
        lst.condo_confidence = lst.condo_confidence or info.year_confidence


def _accepts(confidence: str) -> bool:
    """Is this good enough to write a year to Notion?"""
    floor = _CONF_ORDER.get(settings.condo_search_min_confidence, 2)
    return _CONF_ORDER.get(confidence, 0) >= floor


def _lookup_year(lst: Listing):
    """Run the configured resolver for one building.

    Returns an object exposing `.year`, `.confidence`, `.url`, plus (agent only)
    `.name`, `.notes`, `.conflicting_years` — or None when the resolver could
    not be reached at all.
    """
    if settings.condo_search_provider == "claude":
        from .condo_agent import lookup_condo

        return lookup_condo(
            name=lst.condo_name or lst.condo_name_raw,
            street=lst.condo_street,
            number=lst.condo_number,
            neighborhood=lst.neighborhood,
            hints={
                "condo_fee": lst.condo_fee,
                "sqm": lst.sqm,
                "bedrooms": lst.bedrooms,
                "suites": lst.suites,
                "parking": lst.parking,
            },
        )

    from .condo_search import search_year

    # The SERP tier can only work from a name it already has.
    if not looks_like_name(lst.condo_name or lst.condo_name_raw):
        return None
    return search_year(
        name=search_name(lst.condo_name or lst.condo_name_raw),
        street=lst.condo_street,
        number=lst.condo_number,
        neighborhood=lst.neighborhood,
    )


def resolve_condo(lst: Listing, *, allow_search: bool = True) -> None:
    """Fill condo name / built year / age / confidence.

    Resolution itself never drops anything; the age gate in the pipeline reads
    what lands here and is the only place condo data rejects a listing.
    """
    if not lst.condo_key:
        log.info("condo_key_missing", id=lst.global_id, condo=lst.condo_name_raw)
        return
    reg = registry()
    info = reg.lookup(lst.condo_key)
    # A backfill row carries only the key, so the address the resolver needs has
    # to come from the registry — or, failing that, out of the key itself. The
    # SERP tier silently searched with an empty street on that path.
    if info:
        lst.condo_street = lst.condo_street or info.street
        lst.condo_number = lst.condo_number or info.number
    if not (lst.condo_street and lst.condo_number):
        street, number = split_key(lst.condo_key)
        lst.condo_street = lst.condo_street or street
        lst.condo_number = lst.condo_number or number
    if info and info.blocked:
        lst.condo_confidence = "not_found"
        return
    _apply_registry(lst, info)
    if lst.built_year:
        lst.apply_built_year(lst.built_year)
        # only QuintoAndar serves a year natively; anything else came via registry
        lst.condo_source = lst.condo_source or "quintoandar"
        if not lst.condo_confidence:
            # QA serves the year first-party but never says which address it read
            # it off, so it is "medium" at best. A year of unknown provenance is
            # an estimate and must not reach a ranking formula unqualified.
            lst.condo_confidence = (
                "medium" if lst.condo_source == "quintoandar" else "estimated"
            )
        return
    if not (allow_search and settings.condo_search_enabled):
        return
    if not reg.claim_search(lst.condo_key):
        # Another listing in this same building is being resolved right now.
        # Wait for it and take its answer rather than leaving this row blank.
        reg.wait_for_search(lst.condo_key, settings.condo_agent_timeout_sec + 30)
        _apply_registry(lst, reg.lookup(lst.condo_key))
        if not lst.condo_confidence:
            lst.condo_confidence = "not_found"
        return
    try:
        if not _take_search_slot():
            return
        source = "claude" if settings.condo_search_provider == "claude" else "search"

        with _search_budget:
            hit = _lookup_year(lst)

        if hit is None:
            # Nothing came back — a transport failure, not an answer. Leave
            # `searched_at` alone so the building is retried next run.
            return

        name = getattr(hit, "name", "") or ""
        confidence = getattr(hit, "confidence", "") or ""
        accepted = bool(hit.year) and _accepts(confidence)
        reg.learn(
            lst.condo_key,
            name=name or None,
            built_year=hit.year if accepted else None,
            source=source,
            searched_at=date.today().isoformat(),
            year_confidence=confidence,
            year_source_url=getattr(hit, "url", "") or "",
            year_notes=getattr(hit, "notes", "") or "",
            year_conflicts=",".join(
                str(y) for y in getattr(hit, "conflicting_years", ()) or ()
            ),
        )
        if name and not lst.condo_name:
            lst.condo_name = clean_condo_name(name)
            lst.condo_name_raw = lst.condo_name_raw or name
        lst.condo_confidence = confidence
        if accepted:
            lst.apply_built_year(hit.year)
            lst.condo_source = source
        elif hit.year:
            log.info(
                "condo_year_below_floor",
                key=lst.condo_key,
                year=hit.year,
                confidence=confidence,
                floor=settings.condo_search_min_confidence,
            )
    finally:
        reg.release_search(lst.condo_key)
