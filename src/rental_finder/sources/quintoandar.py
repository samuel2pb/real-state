from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterator

import structlog

from ..config import settings
from ..http_client import CurlSession
from ..models import Listing
from .base import Source

log = structlog.get_logger(__name__)

BASE = "https://www.quintoandar.com.br"
_NEXT_RE = re.compile(r"__NEXT_DATA__[^>]*>(.*?)</script>", re.DOTALL)

_PROPERTY_TYPE_MAP = {
    "apartment": "apartamento",
    "house": "casa",
    "condo_house": "casacondominio",
}


def _slugify(text: str) -> str:
    """Convert 'Vila Nova Conceição' → 'vila-nova-conceicao'."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


class QuintoAndarSource(Source):
    name = "quintoandar"

    def __init__(self) -> None:
        self.http = CurlSession(
            "quintoandar",
            base_headers={
                "Origin": BASE,
                "Referer": BASE + "/",
            },
        )

    def _search_url(self, neighborhood: str) -> str:
        slug = _slugify(neighborhood)
        city_slug = _slugify(settings.rent_city)
        state = settings.rent_state.lower()

        segments = [f"alugar/imovel/{slug}-{city_slug}-{state}-brasil"]

        type_str = settings.rent_property_types.strip()
        if type_str:
            for t in type_str.split(","):
                slug_t = _PROPERTY_TYPE_MAP.get(t.strip())
                if slug_t:
                    segments.append(slug_t)

        segments.append(
            f"de-{settings.rent_price_min}-a-{settings.rent_price_max}-reais"
        )

        beds = str(settings.rent_bedrooms_min)
        segments.append(f"{beds}-quartos")

        parks = "-".join(
            str(p)
            for p in range(settings.rent_parking_min, settings.rent_parking_min + 3)
        )
        segments.append(f"{parks}-vagas")

        baths = "-".join(
            str(b)
            for b in range(settings.rent_bathrooms_min, settings.rent_bathrooms_min + 3)
        )
        segments.append(f"{baths}-banheiros")

        segments.append(f"de-{settings.rent_sqm_min}-a-1000-m2")

        if not settings.rent_furnished_allowed:
            segments.append("nao-mobiliado")

        if settings.rent_pets_required:
            segments.append("aceita-pets")

        if settings.rent_suites_min > 0:
            segments.append(f"{settings.rent_suites_min}-suites")

        return BASE + "/" + "/".join(segments)

    def _extract_next_data(self, html: str) -> dict | None:
        m = _NEXT_RE.search(html)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return None

    def _fetch_search_houses(self, neighborhood: str) -> dict[str, dict]:
        """Fetch SSR search page and return {house_id: house_dict}."""
        seen: dict[str, dict] = {}
        url = self._search_url(neighborhood)
        try:
            r = self.http.get(url, headers={"Accept": "text/html"})
        except Exception as e:
            log.warning("qa_ssr_err", nb=neighborhood, err=str(e))
            return seen
        nd = self._extract_next_data(r.text)
        if not nd:
            log.warning("qa_no_nextdata", nb=neighborhood, url=url)
            return seen
        houses = (
            nd.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("houses", {})
        )
        for hid, hdata in houses.items():
            if hid.isdigit() and isinstance(hdata, dict):
                seen[hid] = hdata
        total = (
            nd.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("search", {})
            .get("visibleHouses", {})
            .get("total", 0)
        )
        log.info(
            "qa_ssr",
            nb=neighborhood,
            hits=len(seen),
            total=total,
        )
        return seen

    def _fetch_detail(self, house_id: str) -> dict[str, Any] | None:
        """Fetch individual listing page for lat/lng and full houseInfo."""
        url = f"{BASE}/imovel/{house_id}"
        try:
            r = self.http.get(url, headers={"Accept": "text/html"})
        except Exception as e:
            log.warning("qa_detail_err", id=house_id, err=str(e))
            return None
        nd = self._extract_next_data(r.text)
        if not nd:
            return None
        return (
            nd.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("house", {})
        )

    def _parse_search_hit(
        self, hid: str, raw: dict[str, Any], nb: str
    ) -> Listing | None:
        total = float(raw.get("totalCost") or 0)
        rent = float(raw.get("rentPrice") or 0)
        if total == 0:
            total = rent
        neighborhood = raw.get("neighbourhood") or raw.get("regionName") or nb
        amenities = [a.lower() for a in (raw.get("amenities") or [])]
        addr = raw.get("address") or ""
        return Listing(
            source=self.name,
            external_id=hid,
            url=f"{BASE}/imovel/{hid}",
            neighborhood=neighborhood,
            price_rent=rent,
            price_total=total,
            bedrooms=int(raw.get("bedrooms") or 0),
            bathrooms=int(raw.get("bathrooms") or 0),
            suites=0,
            parking=int(raw.get("parkingSpots") or 0),
            sqm=float(raw.get("area") or 0),
            pets="aceita pets" in amenities or "pets_allowed" in amenities,
            furnished=bool(raw.get("isFurnished")),
            lat=None,
            lng=None,
            address=f"{addr}, {neighborhood}" if addr else neighborhood,
        )

    def search_rent(self, neighborhoods: list[str]) -> Iterator[Listing]:
        for nb in neighborhoods:
            houses = self._fetch_search_houses(nb)
            log.info("qa_nb_done", nb=nb, unique_houses=len(houses))
            for hid, hdata in houses.items():
                lst = self._parse_search_hit(hid, hdata, nb)
                if not lst:
                    continue
                detail = self._fetch_detail(hid)
                if detail:
                    markers = detail.get("markers") or {}
                    if markers.get("lat") and markers.get("lng"):
                        lst.lat = float(markers["lat"])
                        lst.lng = float(markers["lng"])
                    hi = detail.get("houseInfo") or {}
                    if hi.get("condoPrice"):
                        condo = float(hi["condoPrice"])
                        iptu = float(hi.get("iptu") or 0)
                        lst.price_total = lst.price_rent + condo + iptu
                    if hi.get("suites"):
                        lst.suites = int(hi["suites"])
                    if hi.get("acceptsPets") is not None:
                        lst.pets = bool(hi["acceptsPets"])
                    if hi.get("hasFurniture") is not None:
                        lst.furnished = bool(hi["hasFurniture"])
                    if hi.get("type"):
                        lst.property_type = hi["type"]
                yield lst

    def _buy_property_type_slugs(self) -> list[str]:
        type_str = settings.buy_property_types.strip()
        if not type_str:
            return [""]  # no type filter
        slugs = []
        for t in type_str.split(","):
            slug_t = _PROPERTY_TYPE_MAP.get(t.strip())
            if slug_t:
                slugs.append(slug_t)
        return slugs or [""]

    def _search_buy_url(self, neighborhood: str, type_slug: str = "") -> str:
        slug = _slugify(neighborhood)
        city_slug = _slugify(settings.buy_city)
        state = settings.buy_state.lower()

        segments = [f"comprar/imovel/{slug}-{city_slug}-{state}-brasil"]

        if type_slug:
            segments.append(type_slug)

        segments.append(f"{settings.buy_bedrooms_min}-quartos")

        parks = "-".join(
            str(p)
            for p in range(settings.buy_parking_min, settings.buy_parking_min + 3)
        )
        segments.append(f"{parks}-vagas")

        baths = "-".join(
            str(b)
            for b in range(settings.buy_bathrooms_min, settings.buy_bathrooms_min + 3)
        )
        segments.append(f"{baths}-banheiros")

        segments.append(f"de-{settings.buy_sqm_min}-a-1000-m2")

        if not settings.buy_furnished_allowed:
            segments.append("nao-mobiliado")

        if settings.buy_suites_min > 0:
            segments.append(f"{settings.buy_suites_min}-suites")

        # Sale price uses '-venda' suffix on QuintoAndar buy URLs (not '-reais')
        segments.append(
            f"de-{settings.buy_price_min}-a-{settings.buy_price_max}-venda"
        )

        if settings.buy_condo_max > 0:
            segments.append(f"de-0-a-{settings.buy_condo_max}-condo-iptu")

        return BASE + "/" + "/".join(segments)

    def _fetch_buy_search_houses(self, neighborhood: str) -> dict[str, dict]:
        seen: dict[str, dict] = {}
        for type_slug in self._buy_property_type_slugs():
            url = self._search_buy_url(neighborhood, type_slug)
            try:
                r = self.http.get(url, headers={"Accept": "text/html"})
            except Exception as e:
                log.warning("qa_buy_ssr_err", nb=neighborhood, type=type_slug, err=str(e))
                continue
            nd = self._extract_next_data(r.text)
            if not nd:
                log.warning("qa_buy_no_nextdata", nb=neighborhood, url=url)
                continue
            houses = (
                nd.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("houses", {})
            )
            for hid, hdata in houses.items():
                if hid.isdigit() and isinstance(hdata, dict) and hid not in seen:
                    seen[hid] = hdata
            total = (
                nd.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("search", {})
                .get("visibleHouses", {})
                .get("total", 0)
            )
            log.info(
                "qa_buy_ssr", nb=neighborhood, type=type_slug, hits=len(houses), total=total
            )
        return seen

    def _parse_buy_search_hit(
        self, hid: str, raw: dict[str, Any], nb: str
    ) -> Listing | None:
        sale_price = float(raw.get("salePrice") or 0)
        if sale_price <= 0:
            return None
        condo_iptu = float(raw.get("condoIptu") or raw.get("totalCost") or 0)
        neighborhood = raw.get("neighbourhood") or raw.get("regionName") or nb
        addr_raw = raw.get("address")
        if isinstance(addr_raw, dict):
            addr_str = addr_raw.get("address") or ""
        else:
            addr_str = str(addr_raw or "")
        return Listing(
            source=self.name,
            mode="buy",
            external_id=hid,
            url=f"{BASE}/imovel/{hid}/comprar",
            neighborhood=neighborhood,
            price_rent=0.0,
            price_total=sale_price,
            price_sale=sale_price,
            condo_fee=condo_iptu,
            iptu=0.0,
            bedrooms=int(raw.get("bedrooms") or 0),
            bathrooms=int(raw.get("bathrooms") or 0),
            suites=0,
            parking=int(raw.get("parkingSpots") or 0),
            sqm=float(raw.get("area") or 0),
            pets=False,
            furnished=bool(raw.get("isFurnished")),
            property_type=str(raw.get("type") or ""),
            lat=None,
            lng=None,
            address=f"{addr_str}, {neighborhood}" if addr_str else neighborhood,
        )

    def _fetch_buy_detail(self, house_id: str) -> dict[str, Any] | None:
        # The bare /imovel/<id> URL 301-redirects to /imovel/<id>/alugar/<slug>
        # which 404s for sale-only listings. /imovel/<id>/comprar resolves to
        # the sale detail page via a 301 to its slugged form.
        url = f"{BASE}/imovel/{house_id}/comprar"
        try:
            r = self.http.get(url, headers={"Accept": "text/html"})
        except Exception as e:
            log.warning("qa_buy_detail_err", id=house_id, err=str(e))
            return None
        nd = self._extract_next_data(r.text)
        if not nd:
            return None
        return (
            nd.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("house", {})
        )

    def search_buy(self, neighborhoods: list[str]) -> Iterator[Listing]:
        for nb in neighborhoods:
            houses = self._fetch_buy_search_houses(nb)
            log.info("qa_buy_nb_done", nb=nb, unique_houses=len(houses))
            for hid, hdata in houses.items():
                lst = self._parse_buy_search_hit(hid, hdata, nb)
                if not lst:
                    continue
                detail = self._fetch_buy_detail(hid)
                if detail:
                    markers = detail.get("markers") or {}
                    if markers.get("lat") and markers.get("lng"):
                        lst.lat = float(markers["lat"])
                        lst.lng = float(markers["lng"])
                    hi = detail.get("houseInfo") or {}
                    if hi.get("condoPrice"):
                        lst.condo_fee = float(hi["condoPrice"])
                    if hi.get("iptu"):
                        lst.iptu = float(hi["iptu"])
                    if hi.get("suites"):
                        lst.suites = int(hi["suites"])
                    if hi.get("type"):
                        lst.property_type = hi["type"]
                yield lst

    def check_alive(self, listing: Listing) -> bool:
        try:
            r = self.http.get(
                listing.url, allow_redirects=True, headers={"Accept": "text/html"}
            )
            if r.status_code == 404:
                return False
            body = (r.text or "").lower()
            gone_markers = (
                "imóvel não encontrado",
                "não está mais disponível",
                "alugado",
            )
            return not any(m in body for m in gone_markers)
        except Exception as e:
            log.warning("qa_alive_err", id=listing.external_id, err=str(e))
            return True
