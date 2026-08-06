from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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
    first_seen: date = field(default_factory=date.today)
    last_seen: date = field(default_factory=date.today)
    status: str = "available"  # "available" | "gone" | "price-up" | "price-drop"
    mode: str = "rent"          # "rent" | "buy"
    price_sale: float = 0.0     # sale price (zero for rent listings)
    condo_fee: float = 0.0      # monthly condo (display only)
    iptu: float = 0.0           # IPTU as sourced (annual on zap, unknown on qa)
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
    condo_source: str = ""          # "quintoandar" | "search" | "manual"

    @property
    def global_id(self) -> str:
        return f"{self.mode}:{self.source}:{self.external_id}"

    def apply_built_year(self, year: int | None) -> None:
        """Set built_year and (re)derive age. Age is never negative nor stale."""
        self.built_year = year
        self.condo_age_years = (
            max(0, date.today().year - year) if year else None
        )
