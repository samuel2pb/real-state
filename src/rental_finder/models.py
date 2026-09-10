from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone


@dataclass(slots=True)
class Listing:
    source: str  # "zap" | "quintoandar"
    external_id: str  # unique per source
    url: str
    neighborhood: str
    price_rent: float  # just the rent
    price_total: float  # rent + condo + iptu
    bedrooms: int
    bathrooms: int
    suites: int
    parking: int
    sqm: float
    pets: bool
    furnished: bool | None
    lat: float | None
    lng: float | None
    property_type: str = ""
    address: str = ""
    distance_km: float | None = None
    listed_at: date | None = None  # date the ad was published at the source
    first_seen: date = field(default_factory=date.today)
    last_seen: date = field(default_factory=date.today)
    status: str = "available"  # "available" | "gone" | "price-up" | "price-drop"
    mode: str = "rent"          # "rent" | "buy"
    price_sale: float = 0.0     # sale price (zero for rent listings)
    condo_fee: float = 0.0      # monthly condo (display only)
    iptu: float = 0.0           # IPTU as sourced (annual on zap, unknown on qa)
    # Advertised total area, when the source publishes it separately. Zap sends
    # usableAreas + totalAreas; QuintoAndar sends one unlabelled `area`, which
    # is how 141 m2 (unit + two parking spaces) was stored as the unit size of a
    # ~119 m2 apartment. `sqm_basis` says which of those `sqm` actually is.
    sqm_total: float = 0.0
    sqm_basis: str = ""         # "usable" | "unspecified"
    prev_price: float | None = None      # price recorded before this drop
    price_drop: float | None = None      # absolute BRL amount dropped (positive)
    price_drop_pct: float | None = None  # percent dropped (positive, e.g. 4.7)
    condo_name: str = ""            # cleaned building name, e.g. "Twins Towers"
    condo_name_raw: str = ""        # as sourced, kept for debugging
    condo_key: str = ""             # "04524020|191" (CEP + street number)
    condo_street: str = ""          # building street, e.g. "Avenida Ibijaú"
    condo_number: str = ""          # building street number, e.g. "191"
    condo_hash: str = ""            # QuintoAndar condominium.hashId
    built_year: int | None = None   # construction / delivery year
    condo_age_years: int | None = None
    condo_source: str = ""          # "quintoandar" | "claude" | "search" | "manual"
    # "high" | "medium" | "estimated" | "not_found". Ranking must ignore
    # CondoAge below "medium": an unqualified year is how a 1977 building got
    # recorded as 2023 and topped the base.
    condo_confidence: str = ""

    @property
    def global_id(self) -> str:
        return f"{self.mode}:{self.source}:{self.external_id}"

    def apply_built_year(self, year: int | None) -> None:
        """Set built_year and (re)derive age. Age is never negative nor stale."""
        self.built_year = year
        self.condo_age_years = (
            max(0, date.today().year - year) if year else None
        )


def parse_source_date(value: object) -> date | None:
    """Best-effort source timestamp -> date. Returns None when unusable.

    Handles ISO-8601 with or without the trailing 'Z' ("2025-08-28T18:56:59.671Z"),
    plain "YYYY-MM-DD", and epoch seconds/milliseconds.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return parse_source_date(int(raw))
        raw = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None
