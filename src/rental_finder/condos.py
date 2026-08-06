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

# Higher wins. QuintoAndar outranks search: first-party structured data beats a
# SERP snippet. Manual overrides beat everything.
SOURCE_RANK = {"": 0, "tpcl": 1, "search": 2, "quintoandar": 3, "manual": 4}

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
                self.learn(
                    key,
                    name=(row.get("name") or "").strip() or None,
                    built_year=int(year) if year else None,
                    source="manual",
                    year_confidence="high",
                    persist=False,
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
        with self._lock:
            info = self._data.setdefault(key, CondoInfo(key=key))
            if name and rank >= SOURCE_RANK.get(info.name_source, 0):
                info.name_raw = meta.get("name_raw") or name
                info.name = clean_condo_name(name)
                info.name_source = source
            if built_year and rank >= SOURCE_RANK.get(info.year_source, 0):
                info.built_year = built_year
                info.year_source = source
                info.year_confidence = meta.get("year_confidence") or ""
                info.year_source_url = meta.get("year_source_url") or ""
            for attr in ("street", "number", "cep", "qa_hash", "searched_at"):
                val = meta.get(attr)
                if val:
                    setattr(info, attr, str(val))
            info.updated = date.today().isoformat()
            if persist:
                self._flush()

    def needs_search(self, key: str) -> bool:
        """True when this building has no year and is not in the retry cooldown."""
        info = self.lookup(key)
        if info is None:
            return True
        if info.built_year:
            return False
        if not info.searched_at:
            return True
        try:
            last = date.fromisoformat(info.searched_at)
        except ValueError:
            return True
        return date.today() - last >= timedelta(days=settings.condo_search_retry_days)

    def stats(self) -> dict:
        with self._lock:
            rows = list(self._data.values())
        by_source: dict[str, int] = {}
        for r in rows:
            if r.built_year:
                by_source[r.year_source or "?"] = by_source.get(r.year_source or "?", 0) + 1
        return {
            "buildings": len(rows),
            "with_name": sum(1 for r in rows if r.name),
            "with_year": sum(1 for r in rows if r.built_year),
            "by_year_source": by_source,
        }


_registry: CondoRegistry | None = None
_registry_lock = threading.Lock()
_search_budget = threading.Semaphore(1)
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


def resolve_condo(lst: Listing, *, allow_search: bool = True) -> None:
    """Fill condo name / built year / age on a listing. Never filters it out."""
    if not lst.condo_key:
        log.info("condo_key_missing", id=lst.global_id, condo=lst.condo_name_raw)
        return
    reg = registry()
    info = reg.lookup(lst.condo_key)
    if info:
        if info.name:
            lst.condo_name = info.name
            lst.condo_name_raw = lst.condo_name_raw or info.name_raw
        if info.built_year and not lst.built_year:
            lst.apply_built_year(info.built_year)
            lst.condo_source = info.year_source
        elif info.built_year and not lst.condo_source:
            lst.condo_source = info.year_source
    if lst.built_year:
        lst.apply_built_year(lst.built_year)
        # only QuintoAndar serves a year natively; anything else came via registry
        lst.condo_source = lst.condo_source or "quintoandar"
        return
    if not (allow_search and settings.condo_search_enabled):
        return
    if not reg.needs_search(lst.condo_key) or not _take_search_slot():
        return

    from .condo_search import search_year

    with _search_budget:
        hit = search_year(
            name=search_name(lst.condo_name or lst.condo_name_raw),
            street=lst.condo_street,
            number=lst.condo_number,
            neighborhood=lst.neighborhood,
        )
    reg.learn(
        lst.condo_key,
        built_year=hit.year if hit else None,
        source="search",
        searched_at=date.today().isoformat(),
        year_confidence=hit.confidence if hit else "",
        year_source_url=hit.url if hit else "",
    )
    if hit:
        lst.apply_built_year(hit.year)
        lst.condo_source = "search"
