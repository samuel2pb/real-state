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
    status: str = "available"  # "available" | "gone"
    mode: str = "rent"          # "rent" | "buy"
    price_sale: float = 0.0     # sale price (zero for rent listings)
    condo_fee: float = 0.0      # monthly condo (display only)
    iptu: float = 0.0           # IPTU as sourced (annual on zap, unknown on qa)

    @property
    def global_id(self) -> str:
        return f"{self.mode}:{self.source}:{self.external_id}"
