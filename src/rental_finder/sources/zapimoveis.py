from __future__ import annotations

import json
from typing import Any, Iterator

import structlog

from ..config import settings
from ..http_client import CurlSession
from ..models import Listing
from .base import Source

log = structlog.get_logger(__name__)

GLUE_API = "https://glue-api.zapimoveis.com.br/v2/listings"
BASE = "https://www.zapimoveis.com.br"
INCLUDE = (
    "search,page,seasonalCampaigns,fullUriFragments,nearby,expansion,"
    "accountPages,developments,superPremium,schema,owners,stamps"
)

# SP city zones to probe for each bairro; first hit w/ totalCount>0 wins.
ZONE_CANDIDATES = ["Zona Sul", "Zona Oeste", "Zona Norte", "Zona Leste", "Centro"]

_ZAP_PROPERTY_TYPE_MAP = {
    "apartment": "APARTMENT",
    "house": "HOME",
    "condo_house": "CONDOMINIUM",
}

# Preloaded hints (admin zone consensus for the 9 target bairros). Still verified at runtime.
ZONE_HINTS = {
    "Moema": "Zona Sul",
    "Campo Belo": "Zona Sul",
    "Brooklin": "Zona Sul",
    "Vila Nova Conceição": "Zona Sul",
    "Vila Olímpia": "Zona Sul",
    "Itaim Bibi": "Zona Oeste",
    "Pinheiros": "Zona Oeste",
    "Jardim Paulista": "Zona Oeste",
    "Jardim Paulistano": "Zona Oeste",
}


def _location_id(neighborhood: str, zone: str) -> str:
    return f"BR>Sao Paulo>NULL>Sao Paulo>{zone}>{neighborhood}"


def _zap_unit_types() -> list[str]:
    """Resolve configured property types to Zap unitTypes values."""
    raw = settings.rent_property_types.strip()
    if not raw:
        return ["APARTMENT"]
    types = []
    for t in raw.split(","):
        mapped = _ZAP_PROPERTY_TYPE_MAP.get(t.strip())
        if mapped:
            types.append(mapped)
    return types or ["APARTMENT"]


def _zap_buy_unit_types() -> list[str]:
    """Resolve configured buy property types to Zap unitTypes values."""
    raw = settings.buy_property_types.strip()
    if not raw:
        return ["APARTMENT"]
    types = []
    for t in raw.split(","):
        mapped = _ZAP_PROPERTY_TYPE_MAP.get(t.strip())
        if mapped:
            types.append(mapped)
    return types or ["APARTMENT"]


class ZapSource(Source):
    name = "zap"

    def __init__(self) -> None:
        self.http = CurlSession(
            "zap",
            base_headers={
                "x-domain": "www.zapimoveis.com.br",
                "Origin": BASE,
                "Referer": BASE + "/",
            },
        )
        self._zone_cache_file = settings.cache_path / "zap_zones.json"
        self._zone_cache: dict[str, str] = {}
        if self._zone_cache_file.exists():
            try:
                self._zone_cache = json.loads(self._zone_cache_file.read_text("utf-8"))
            except Exception:
                self._zone_cache = {}

    def _resolve_zone(self, neighborhood: str) -> str:
        if neighborhood in self._zone_cache:
            return self._zone_cache[neighborhood]
        order: list[str] = []
        hint = ZONE_HINTS.get(neighborhood)
        if hint:
            order.append(hint)
        for z in ZONE_CANDIDATES:
            if z not in order:
                order.append(z)
        for zone in order:
            try:
                r = self.http.get(
                    GLUE_API,
                    params=[
                        ("portal", "ZAP"),
                        ("business", "RENTAL"),
                        ("listingType", "USED"),
                        ("addressLocationId", _location_id(neighborhood, zone)),
                        *[("unitTypes", ut) for ut in _zap_unit_types()],
                        ("usageTypes", "RESIDENTIAL"),
                        ("size", "1"),
                        ("from", "0"),
                        ("page", "1"),
                        ("levels", "NEIGHBORHOOD"),
                        ("includeFields", "search"),
                    ],
                )
                total = ((r.json().get("search") or {}).get("totalCount")) or 0
            except Exception as e:
                log.warning(
                    "zap_zone_probe_err", nb=neighborhood, zone=zone, err=str(e)
                )
                continue
            log.info("zap_zone_probe", nb=neighborhood, zone=zone, total=total)
            if total > 0:
                self._zone_cache[neighborhood] = zone
                self._zone_cache_file.write_text(
                    json.dumps(self._zone_cache, ensure_ascii=False, indent=2), "utf-8"
                )
                return zone
        raise RuntimeError(f"zap: no zone produced results for {neighborhood!r}")

    def _params(
        self, neighborhood: str, zone: str, page: int, size: int = 60
    ) -> list[tuple[str, str]]:
        amenities = "PETS_ALLOWED" if settings.rent_pets_required else ""
        p: list[tuple[str, str]] = [
            ("user", ""),
            ("portal", "ZAP"),
            ("includeFields", INCLUDE),
            ("business", "RENTAL"),
            ("listingType", "USED"),
            ("categoryPage", "RESULT"),
            ("parentId", "null"),
            ("addressCountry", "Brasil"),
            ("addressState", settings.rent_state),
            ("addressCity", settings.rent_city),
            ("addressType", "neighborhood"),
            ("addressLocationId", _location_id(neighborhood, zone)),
            ("addressNeighborhood", neighborhood),
            *[("unitTypes", ut) for ut in _zap_unit_types()],
            ("usageTypes", "RESIDENTIAL"),
            ("rentalTotalPriceMin", str(settings.rent_price_min)),
            ("rentalTotalPriceMax", str(settings.rent_price_max)),
            ("bedroomsMin", str(settings.rent_bedrooms_min)),
            ("bedroomsMax", str(settings.rent_bedrooms_max)),
            ("bathrooms", str(settings.rent_bathrooms_min)),
            ("suites", str(settings.rent_suites_min)),
            ("parkingSpaces", str(settings.rent_parking_min)),
            ("usableAreasMin", str(settings.rent_sqm_min)),
            ("from", str((page - 1) * size)),
            ("size", str(size)),
            ("page", str(page)),
            ("levels", "NEIGHBORHOOD"),
            ("sort", "pricing.rentalTotalPrice.price ASC"),
        ]
        if amenities:
            p.append(("amenities", amenities))
        return p

    def _buy_params(
        self, neighborhood: str, zone: str, page: int, size: int = 60
    ) -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = [
            ("user", ""),
            ("portal", "ZAP"),
            ("includeFields", INCLUDE),
            ("business", "SALE"),
            ("listingType", "USED"),
            ("categoryPage", "RESULT"),
            ("parentId", "null"),
            ("addressCountry", "Brasil"),
            ("addressState", settings.rent_state),
            ("addressCity", settings.rent_city),
            ("addressType", "neighborhood"),
            ("addressLocationId", _location_id(neighborhood, zone)),
            ("addressNeighborhood", neighborhood),
            *[("unitTypes", ut) for ut in _zap_buy_unit_types()],
            ("usageTypes", "RESIDENTIAL"),
            ("priceMin", str(settings.buy_price_min)),
            ("priceMax", str(settings.buy_price_max)),
            ("bedroomsMin", str(settings.rent_bedrooms_min)),
            ("bedroomsMax", str(settings.rent_bedrooms_max)),
            ("bathrooms", str(settings.rent_bathrooms_min)),
            ("suites", str(settings.rent_suites_min)),
            ("parkingSpaces", str(settings.rent_parking_min)),
            ("usableAreasMin", str(settings.rent_sqm_min)),
            ("from", str((page - 1) * size)),
            ("size", str(size)),
            ("page", str(page)),
            ("levels", "NEIGHBORHOOD"),
            ("sort", "pricing.price.price ASC"),
        ]
        if settings.rent_pets_required:
            p.append(("amenities", "PETS_ALLOWED"))
        return p

    def _parse_one(
        self, raw: dict[str, Any], neighborhood_fallback: str
    ) -> Listing | None:
        node = raw.get("listing") or {}
        ext_id = node.get("id") or raw.get("id") or ""
        if not ext_id:
            return None
        pricings = node.get("pricingInfos") or []
        rental = next(
            (p for p in pricings if p.get("businessType") == "RENTAL"),
            pricings[0] if pricings else {},
        )
        price = float(rental.get("price") or 0)
        rinfo = rental.get("rentalInfo") or {}
        total = float(
            rinfo.get("monthlyRentalTotalPrice")
            or rental.get("monthlyRentalTotalPrice")
            or price
        )
        condo = float(rental.get("monthlyCondoFee") or 0)
        if total == 0 and price:
            total = price + condo
        addr = node.get("address") or {}
        geo = (addr.get("geoLocation") or {}).get("location") or {}
        link = raw.get("link") or {}
        href = link.get("href") or ""
        url = (
            f"{BASE}{href}"
            if href.startswith("/")
            else href or f"{BASE}/imovel/{ext_id}/"
        )
        amenities = [a.upper() for a in (node.get("amenities") or [])]
        pets = "PETS_ALLOWED" in amenities
        furnished = "FURNISHED" in amenities
        unit_types = node.get("unitTypes") or []
        prop_type = unit_types[0] if unit_types else ""
        return Listing(
            source=self.name,
            external_id=str(ext_id),
            url=url,
            neighborhood=addr.get("neighborhood") or neighborhood_fallback,
            price_rent=price,
            price_total=total,
            bedrooms=int((node.get("bedrooms") or [0])[0] or 0),
            bathrooms=int((node.get("bathrooms") or [0])[0] or 0),
            suites=int((node.get("suites") or [0])[0] or 0),
            parking=int((node.get("parkingSpaces") or [0])[0] or 0),
            sqm=float((node.get("usableAreas") or [0])[0] or 0),
            pets=pets,
            furnished=furnished,
            property_type=prop_type,
            lat=float(geo["lat"]) if geo.get("lat") else None,
            lng=float(geo["lon"]) if geo.get("lon") else None,
            address=", ".join(
                filter(
                    None,
                    [addr.get("street"), addr.get("neighborhood"), addr.get("city")],
                )
            ),
        )

    def _parse_buy_one(
        self, raw: dict[str, Any], neighborhood_fallback: str
    ) -> Listing | None:
        node = raw.get("listing") or {}
        ext_id = node.get("id") or raw.get("id") or ""
        if not ext_id:
            return None
        pricings = node.get("pricingInfos") or []
        sale_info = next(
            (p for p in pricings if p.get("businessType") == "SALE"),
            pricings[0] if pricings else {},
        )
        price_sale = float(sale_info.get("price") or 0)
        condo_fee = float(sale_info.get("monthlyCondoFee") or 0)
        iptu = float(
            sale_info.get("yearlyIptu")
            or sale_info.get("monthlyIptu")
            or 0
        )
        addr = node.get("address") or {}
        geo = (addr.get("geoLocation") or {}).get("location") or {}
        link = raw.get("link") or {}
        href = link.get("href") or ""
        url = (
            f"{BASE}{href}"
            if href.startswith("/")
            else href or f"{BASE}/imovel/{ext_id}/"
        )
        amenities = [a.upper() for a in (node.get("amenities") or [])]
        pets = "PETS_ALLOWED" in amenities
        furnished = "FURNISHED" in amenities
        unit_types = node.get("unitTypes") or []
        prop_type = unit_types[0] if unit_types else ""
        return Listing(
            source=self.name,
            mode="buy",
            external_id=str(ext_id),
            url=url,
            neighborhood=addr.get("neighborhood") or neighborhood_fallback,
            price_rent=0.0,
            price_total=price_sale,
            price_sale=price_sale,
            condo_fee=condo_fee,
            iptu=iptu,
            bedrooms=int((node.get("bedrooms") or [0])[0] or 0),
            bathrooms=int((node.get("bathrooms") or [0])[0] or 0),
            suites=int((node.get("suites") or [0])[0] or 0),
            parking=int((node.get("parkingSpaces") or [0])[0] or 0),
            sqm=float((node.get("usableAreas") or [0])[0] or 0),
            pets=pets,
            furnished=furnished,
            property_type=prop_type,
            lat=float(geo["lat"]) if geo.get("lat") else None,
            lng=float(geo["lon"]) if geo.get("lon") else None,
            address=", ".join(
                filter(
                    None,
                    [addr.get("street"), addr.get("neighborhood"), addr.get("city")],
                )
            ),
        )

    def search_rent(self, neighborhoods: list[str]) -> Iterator[Listing]:
        size = 60
        for nb in neighborhoods:
            try:
                zone = self._resolve_zone(nb)
            except Exception as e:
                log.warning("zap_skip_nb", nb=nb, err=str(e))
                continue
            page = 1
            while True:
                r = self.http.get(GLUE_API, params=self._params(nb, zone, page, size))
                data = r.json()
                hits = (
                    ((data.get("search") or {}).get("result") or {}).get("listings")
                ) or []
                log.info("zap_page", nb=nb, page=page, hits=len(hits))
                if not hits:
                    break
                for raw in hits:
                    lst = self._parse_one(raw, nb)
                    if lst:
                        yield lst
                if len(hits) < size:
                    break
                page += 1
                if page > 20:
                    break

    def search_buy(self, neighborhoods: list[str]) -> Iterator[Listing]:
        size = 60
        for nb in neighborhoods:
            try:
                zone = self._resolve_zone(nb)
            except Exception as e:
                log.warning("zap_buy_skip_nb", nb=nb, err=str(e))
                continue
            page = 1
            while True:
                r = self.http.get(GLUE_API, params=self._buy_params(nb, zone, page, size))
                data = r.json()
                hits = (
                    ((data.get("search") or {}).get("result") or {}).get("listings")
                ) or []
                log.info("zap_buy_page", nb=nb, page=page, hits=len(hits))
                if not hits:
                    break
                for raw in hits:
                    lst = self._parse_buy_one(raw, nb)
                    if lst:
                        yield lst
                if len(hits) < size:
                    break
                page += 1
                if page > 20:
                    break

    def check_alive(self, listing: Listing) -> bool:
        try:
            r = self.http.get(listing.url, allow_redirects=True)
            body = (r.text or "").lower()
            gone_markers = (
                "imóvel não encontrado",
                "anúncio removido",
                "este imóvel não está disponível",
            )
            return not any(m in body for m in gone_markers)
        except Exception as e:
            log.warning("zap_alive_err", id=listing.external_id, err=str(e))
            return True


if __name__ == "__main__":
    src = ZapSource()
    for i, lst in enumerate(src.search_rent([settings.neighborhoods_list[0]])):
        print(lst)
        if i >= 2:
            break
