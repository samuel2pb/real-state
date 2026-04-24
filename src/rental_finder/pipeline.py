from __future__ import annotations

from datetime import date

import structlog

from .config import settings
from .geo import distance_km, work_coords
from .models import Listing
from .notion_store import NotionStore
from .sources import enabled_sources

log = structlog.get_logger(__name__)


def _matches_filters(lst: Listing) -> bool:
    s = settings
    if not (s.rent_price_min <= lst.price_total <= s.rent_price_max):
        return False
    if not (s.rent_bedrooms_min <= lst.bedrooms <= s.rent_bedrooms_max):
        return False
    if lst.bathrooms < s.rent_bathrooms_min:
        return False
    if lst.suites < s.rent_suites_min:
        return False
    if lst.sqm < s.rent_sqm_min:
        return False
    if lst.parking < s.rent_parking_min:
        return False
    if s.rent_pets_required and not lst.pets:
        return False
    if not s.rent_furnished_allowed and lst.furnished:
        return False
    return True


def _matches_buy_filters(lst: Listing) -> bool:
    s = settings
    if not (s.buy_price_min <= lst.price_sale <= s.buy_price_max):
        return False
    if s.buy_condo_max > 0 and lst.condo_fee > s.buy_condo_max:
        return False
    if not (s.buy_bedrooms_min <= lst.bedrooms <= s.buy_bedrooms_max):
        return False
    if lst.bathrooms < s.buy_bathrooms_min:
        return False
    if lst.suites < s.buy_suites_min:
        return False
    if lst.sqm < s.buy_sqm_min:
        return False
    if lst.parking < s.buy_parking_min:
        return False
    if s.buy_pets_required and not lst.pets:
        return False
    if not s.buy_furnished_allowed and lst.furnished:
        return False
    return True


def _annotate_distance(lst: Listing, work: tuple[float, float]) -> None:
    if lst.lat is None or lst.lng is None:
        return
    lst.distance_km = round(distance_km(work, (lst.lat, lst.lng)), 3)


def run_rent_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    sources = enabled_sources()
    today = date.today()
    stats = {"fetched": 0, "kept": 0, "upserted": 0, "gone": 0, "per_source": {}}

    fetched_ids: dict[str, set[str]] = {s.name: set() for s in sources}

    for src in sources:
        sc = {"fetched": 0, "kept": 0, "upserted": 0}
        try:
            for lst in src.search_rent(settings.neighborhoods_list):
                sc["fetched"] += 1
                stats["fetched"] += 1
                _annotate_distance(lst, work)
                if (
                    lst.distance_km is not None
                    and lst.distance_km > settings.work_max_distance_km
                ):
                    continue
                if not _matches_filters(lst):
                    continue
                lst.last_seen = today
                lst.first_seen = today
                store.upsert_listing(lst)
                fetched_ids[src.name].add(lst.external_id)
                sc["kept"] += 1
                sc["upserted"] += 1
                stats["kept"] += 1
                stats["upserted"] += 1
        except Exception as e:
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    for alive in store.list_alive():
        src_name = alive["source"]
        if src_name not in src_by_name:
            continue
        if alive["external_id"] in fetched_ids.get(src_name, set()):
            continue
        src = src_by_name[src_name]
        ghost = Listing(
            source=src_name,
            external_id=alive["external_id"],
            url=alive["url"],
            neighborhood=alive["neighborhood"],
            price_rent=0,
            price_total=0,
            bedrooms=0,
            bathrooms=0,
            suites=0,
            parking=0,
            sqm=0,
            pets=False,
            furnished=None,
            lat=None,
            lng=None,
        )
        if not src.check_alive(ghost):
            store.mark_gone(alive["page_id"])
            stats["gone"] += 1
            log.info("marked_gone", id=f"{src_name}:{alive['external_id']}")
        else:
            store.touch_alive(alive["page_id"])
            log.info("still_alive", id=f"{src_name}:{alive['external_id']}")
    return stats


def run_buy_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    sources = enabled_sources(mode="buy")
    today = date.today()
    stats = {"fetched": 0, "kept": 0, "upserted": 0, "gone": 0, "per_source": {}}

    fetched_ids: dict[str, set[str]] = {s.name: set() for s in sources}

    for src in sources:
        sc = {"fetched": 0, "kept": 0, "upserted": 0}
        try:
            for lst in src.search_buy(settings.buy_neighborhoods_list):
                sc["fetched"] += 1
                stats["fetched"] += 1
                _annotate_distance(lst, work)
                if (
                    lst.distance_km is not None
                    and lst.distance_km > settings.work_max_distance_km
                ):
                    continue
                if not _matches_buy_filters(lst):
                    continue
                lst.last_seen = today
                lst.first_seen = today
                store.upsert_listing(lst, kind="buy")
                fetched_ids[src.name].add(lst.external_id)
                sc["kept"] += 1
                sc["upserted"] += 1
                stats["kept"] += 1
                stats["upserted"] += 1
        except Exception as e:
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    for alive in store.list_alive(kind="buy"):
        src_name = alive["source"]
        if src_name not in src_by_name:
            continue
        if alive["external_id"] in fetched_ids.get(src_name, set()):
            continue
        src = src_by_name[src_name]
        ghost = Listing(
            source=src_name,
            mode="buy",
            external_id=alive["external_id"],
            url=alive["url"],
            neighborhood=alive["neighborhood"],
            price_rent=0,
            price_total=0,
            price_sale=0,
            bedrooms=0,
            bathrooms=0,
            suites=0,
            parking=0,
            sqm=0,
            pets=False,
            furnished=None,
            lat=None,
            lng=None,
        )
        if not src.check_alive(ghost):
            store.mark_gone(alive["page_id"])
            stats["gone"] += 1
            log.info("marked_gone", id=ghost.global_id)
        else:
            store.touch_alive(alive["page_id"])
            log.info("still_alive", id=ghost.global_id)
    return stats
