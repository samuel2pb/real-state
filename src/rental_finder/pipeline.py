from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import structlog

from .config import settings
from .geo import distance_km, work_coords
from .models import Listing
from .notion_store import NotionStore
from .sources import enabled_sources
from .sources.base import Source

log = structlog.get_logger(__name__)

POOL_WORKERS = 10


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
        log.info("buy_reject", id=lst.global_id, why="price", v=lst.price_sale)
        return False
    if s.buy_condo_max > 0 and lst.condo_fee > s.buy_condo_max:
        log.info("buy_reject", id=lst.global_id, why="condo", v=lst.condo_fee)
        return False
    if not (s.buy_bedrooms_min <= lst.bedrooms <= s.buy_bedrooms_max):
        log.info("buy_reject", id=lst.global_id, why="bedrooms", v=lst.bedrooms)
        return False
    if lst.bathrooms < s.buy_bathrooms_min:
        log.info("buy_reject", id=lst.global_id, why="bathrooms", v=lst.bathrooms)
        return False
    if lst.suites < s.buy_suites_min:
        log.info("buy_reject", id=lst.global_id, why="suites", v=lst.suites)
        return False
    if lst.sqm < s.buy_sqm_min:
        log.info("buy_reject", id=lst.global_id, why="sqm", v=lst.sqm)
        return False
    if lst.parking < s.buy_parking_min:
        log.info("buy_reject", id=lst.global_id, why="parking", v=lst.parking)
        return False
    if s.buy_pets_required and not lst.pets:
        log.info("buy_reject", id=lst.global_id, why="pets")
        return False
    if not s.buy_furnished_allowed and lst.furnished:
        log.info("buy_reject", id=lst.global_id, why="furnished")
        return False
    return True


def _annotate_distance(lst: Listing, work: tuple[float, float]) -> None:
    if lst.lat is None or lst.lng is None:
        return
    lst.distance_km = round(distance_km(work, (lst.lat, lst.lng)), 3)


def _process_listing(
    lst: Listing,
    src: Source,
    store: NotionStore,
    work: tuple[float, float],
    today: date,
    matches: callable,
    kind: str,
) -> bool:
    """Enrich + filter + upsert one listing. Returns True if upserted."""
    src.enrich(lst)
    _annotate_distance(lst, work)
    if (
        lst.distance_km is not None
        and lst.distance_km > settings.work_max_distance_km
    ):
        if kind == "buy":
            log.info("buy_reject", id=lst.global_id, why="distance", v=lst.distance_km)
        return False
    if not matches(lst):
        return False
    lst.last_seen = today
    lst.first_seen = today
    store.upsert_listing(lst, kind=kind)
    return True


def _run_source(
    src: Source,
    store: NotionStore,
    work: tuple[float, float],
    today: date,
    kind: str,
) -> tuple[dict, set[str]]:
    sc = {"fetched": 0, "kept": 0, "upserted": 0}
    seen: set[str] = set()
    seen_lock = threading.Lock()
    matches = _matches_buy_filters if kind == "buy" else _matches_filters
    search_iter = (
        src.search_buy(settings.buy_neighborhoods_list)
        if kind == "buy"
        else src.search_rent(settings.neighborhoods_list)
    )

    def _job(lst: Listing) -> bool:
        with seen_lock:
            seen.add(lst.external_id)
        return _process_listing(lst, src, store, work, today, matches, kind)

    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as pool:
        futures = []
        for lst in search_iter:
            sc["fetched"] += 1
            futures.append(pool.submit(_job, lst))
        for fut in as_completed(futures):
            try:
                if fut.result():
                    sc["kept"] += 1
                    sc["upserted"] += 1
            except Exception as e:
                log.error("listing_error", source=src.name, err=str(e))
    return sc, seen


def run_rent_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    store.ensure_columns("rent")
    sources = enabled_sources()
    today = date.today()
    stats = {"fetched": 0, "kept": 0, "upserted": 0, "gone": 0, "price_up": 0, "per_source": {}}

    seen_ids: dict[str, set[str]] = {s.name: set() for s in sources}
    completed: set[str] = set()

    for src in sources:
        try:
            sc, seen = _run_source(src, store, work, today, kind="rent")
            seen_ids[src.name] = seen
            stats["fetched"] += sc["fetched"]
            stats["kept"] += sc["kept"]
            stats["upserted"] += sc["upserted"]
            completed.add(src.name)
        except Exception as e:
            sc = {"fetched": 0, "kept": 0, "upserted": 0}
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    for alive in store.list_alive(kind="rent"):
        src_name = alive["source"]
        if src_name not in completed:
            continue
        if alive["external_id"] in seen_ids.get(src_name, set()):
            store.touch_alive(alive["page_id"])
            log.info("still_alive", id=f"{src_name}:{alive['external_id']}")
            continue
        src = src_by_name.get(src_name)
        alive_flag, price = (False, None)
        if src is not None:
            alive_flag, price = src.recheck(
                external_id=alive["external_id"], url=alive["url"], kind="rent"
            )
        if alive_flag and price is not None and price > settings.rent_price_max:
            store.mark_price_up(alive["page_id"], kind="rent", price=price)
            stats["price_up"] += 1
            log.info("marked_price_up", id=f"{src_name}:{alive['external_id']}", price=price)
        else:
            store.mark_gone(alive["page_id"])
            stats["gone"] += 1
            log.info("marked_gone", id=f"{src_name}:{alive['external_id']}")
    return stats


def run_buy_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    store.ensure_columns("buy")
    sources = enabled_sources(mode="buy")
    today = date.today()
    stats = {"fetched": 0, "kept": 0, "upserted": 0, "gone": 0, "price_up": 0, "per_source": {}}

    seen_ids: dict[str, set[str]] = {s.name: set() for s in sources}
    completed: set[str] = set()

    for src in sources:
        try:
            sc, seen = _run_source(src, store, work, today, kind="buy")
            seen_ids[src.name] = seen
            stats["fetched"] += sc["fetched"]
            stats["kept"] += sc["kept"]
            stats["upserted"] += sc["upserted"]
            completed.add(src.name)
        except Exception as e:
            sc = {"fetched": 0, "kept": 0, "upserted": 0}
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    for alive in store.list_alive(kind="buy"):
        src_name = alive["source"]
        if src_name not in completed:
            continue
        if alive["external_id"] in seen_ids.get(src_name, set()):
            store.touch_alive(alive["page_id"])
            log.info("still_alive", id=f"{src_name}:{alive['external_id']}")
            continue
        src = src_by_name.get(src_name)
        alive_flag, price = (False, None)
        if src is not None:
            alive_flag, price = src.recheck(
                external_id=alive["external_id"], url=alive["url"], kind="buy"
            )
        if alive_flag and price is not None and price > settings.buy_price_max:
            store.mark_price_up(alive["page_id"], kind="buy", price=price)
            stats["price_up"] += 1
            log.info("marked_price_up", id=f"{src_name}:{alive['external_id']}", price=price)
        else:
            store.mark_gone(alive["page_id"])
            stats["gone"] += 1
            log.info("marked_gone", id=f"{src_name}:{alive['external_id']}")
    return stats


def recheck_gone(kind: str = "rent") -> dict:
    """Re-examine listings already marked gone; promote true price-up ones.

    Live + current price > max -> mark_price_up. Else leave gone.
    Intended as an occasional backfill, NOT called automatically from run cycles.
    """
    store = NotionStore()
    store.ensure_columns(kind)
    sources = enabled_sources(mode=kind)
    src_by_name = {s.name: s for s in sources}
    pmax = settings.buy_price_max if kind == "buy" else settings.rent_price_max
    stats = {"examined": 0, "price_up": 0, "skipped": 0}

    for gone in store.list_gone(kind=kind):
        stats["examined"] += 1
        src_name = gone["source"]
        src = src_by_name.get(src_name)
        if src is None:
            stats["skipped"] += 1
            continue
        alive_flag, price = src.recheck(
            external_id=gone["external_id"], url=gone["url"], kind=kind
        )
        if alive_flag and price is not None and price > pmax:
            store.mark_price_up(gone["page_id"], kind=kind, price=price)
            stats["price_up"] += 1
            log.info("recheck_priceup", id=f"{src_name}:{gone['external_id']}", price=price)
        else:
            stats["skipped"] += 1

    return stats
