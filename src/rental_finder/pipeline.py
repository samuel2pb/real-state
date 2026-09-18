from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import structlog

from .condos import is_apartment, registry, reset_run_budget, resolve_condo
from .config import settings
from .geo import distance_km, work_coords
from .models import Listing
from .notion_store import NotionStore
from .sources import enabled_sources
from .sources.base import Source

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


def _prefilter_buy(lst: Listing) -> bool:
    """Reject on what the search hit alone already proves, before enriching.

    Every detail fetch sleeps http_min/max_delay_sec first, so an enrich spent
    on a listing the filters then drop is the single most expensive thing a buy
    cycle does — 1090 of 1407 on the last full run. Measured live over two
    neighbourhoods: 75 of 342 dropped here, 267 fetches instead of 341.

    Only fields the search payload fills on the same basis the real filters use.
    Distance is absent (QuintoAndar search hits carry no coordinates), and so
    are suites (sent as 0) and the condo fee (sent as condo+IPTU combined). A
    zero reads as "not stated" rather than "zero", so it never rejects.
    """
    s = settings
    if not (s.buy_price_min <= lst.price_sale <= s.buy_price_max):
        log.info("buy_prefilter", id=lst.global_id, why="price", v=lst.price_sale)
        return False
    if lst.bedrooms and not (
        s.buy_bedrooms_min <= lst.bedrooms <= s.buy_bedrooms_max
    ):
        log.info("buy_prefilter", id=lst.global_id, why="bedrooms", v=lst.bedrooms)
        return False
    if lst.bathrooms and lst.bathrooms < s.buy_bathrooms_min:
        log.info("buy_prefilter", id=lst.global_id, why="bathrooms", v=lst.bathrooms)
        return False
    if lst.sqm and lst.sqm < s.buy_sqm_min:
        log.info("buy_prefilter", id=lst.global_id, why="sqm", v=lst.sqm)
        return False
    if s.buy_pets_required and lst.pets_known and not lst.pets:
        log.info("buy_prefilter", id=lst.global_id, why="pets")
        return False
    if not s.buy_furnished_allowed and lst.furnished:
        log.info("buy_prefilter", id=lst.global_id, why="furnished")
        return False
    return True


def _annotate_distance(lst: Listing, work: tuple[float, float]) -> None:
    if lst.lat is None or lst.lng is None:
        return
    lst.distance_km = round(distance_km(work, (lst.lat, lst.lng)), 3)


def _wants_condo(lst: Listing, kind: str) -> bool:
    return settings.condo_enrich_enabled and kind == "buy" and is_apartment(lst)


def _enrich_and_filter(
    lst: Listing,
    src: Source,
    work: tuple[float, float],
    matches: callable,
    kind: str,
) -> bool:
    """Detail fetch + distance + filters. True when the listing earns a row."""
    src.enrich(lst)
    _annotate_distance(lst, work)
    if (
        lst.distance_km is not None
        and lst.distance_km > settings.work_max_distance_km
    ):
        if kind == "buy":
            log.info("buy_reject", id=lst.global_id, why="distance", v=lst.distance_km)
        return False
    return bool(matches(lst))


def _finalize(
    lst: Listing, store: NotionStore, today: date, kind: str
) -> tuple[bool, float | None]:
    """Condo lookup (kept buy apartments only) + Notion upsert.

    Split out of the enrich path so it can run on a different pool: an agent
    lookup is minutes long, and holding an enrich worker for it stalled the
    detail fetches queued behind it.
    """
    # Only after the listing has earned its Notion row: a condo lookup is an
    # agent run, and spending one on a listing the filters then drop buys
    # nothing. Display/analytics only — never allowed to drop a listing.
    if _wants_condo(lst, kind):
        try:
            resolve_condo(lst)
        except Exception as e:
            log.warning("condo_resolve_err", id=lst.global_id, err=str(e)[:200])
    lst.last_seen = today
    lst.first_seen = today
    store.upsert_listing(lst, kind=kind)
    return True, lst.price_drop


def _process_listing(
    lst: Listing,
    src: Source,
    store: NotionStore,
    work: tuple[float, float],
    today: date,
    matches: callable,
    kind: str,
) -> bool:
    """Enrich + filter + upsert one listing end to end. True if upserted."""
    if not _enrich_and_filter(lst, src, work, matches, kind):
        return False
    _finalize(lst, store, today, kind)
    return True


def _run_source(
    src: Source,
    store: NotionStore,
    work: tuple[float, float],
    today: date,
    kind: str,
) -> tuple[dict, set[str]]:
    sc = {"fetched": 0, "prefiltered": 0, "kept": 0, "upserted": 0, "price_drop": 0}
    seen: set[str] = set()
    seen_lock = threading.Lock()
    matches = _matches_buy_filters if kind == "buy" else _matches_filters
    neighborhoods = (
        settings.buy_neighborhoods_list if kind == "buy" else settings.neighborhoods_list
    )
    tasks = src.search_tasks(neighborhoods, kind)
    prefilter = (
        _prefilter_buy if kind == "buy" and settings.buy_prefilter_enabled else None
    )

    final_futures: list = []
    final_lock = threading.Lock()

    # Two pools on purpose. The enrich pool is sized for network waits, since
    # every detail fetch sleeps http_min/max_delay_sec before it goes out. A
    # condo lookup is a subprocess that runs for minutes and is capped at
    # condo_agent_concurrency regardless, so sharing one pool only meant a
    # couple of lookups sat on enrich workers with every fetch queued behind.
    with ThreadPoolExecutor(max_workers=settings.pool_workers) as pool:
        with ThreadPoolExecutor(max_workers=settings.condo_pool_size) as condo_pool:

            def _job(lst: Listing) -> None:
                if not _enrich_and_filter(lst, src, work, matches, kind):
                    return
                target = condo_pool if _wants_condo(lst, kind) else pool
                fut = target.submit(_finalize, lst, store, today, kind)
                with final_lock:
                    final_futures.append(fut)

            search_futures = [pool.submit(task) for task in tasks]
            enrich_futures = []
            for fut in as_completed(search_futures):
                try:
                    listings = fut.result()
                except Exception as e:
                    log.error("search_task_error", source=src.name, err=str(e))
                    continue
                for lst in listings:
                    sc["fetched"] += 1
                    with seen_lock:
                        seen.add(lst.external_id)
                    if prefilter is not None and not prefilter(lst):
                        sc["prefiltered"] += 1
                        continue
                    enrich_futures.append(pool.submit(_job, lst))
            for fut in as_completed(enrich_futures):
                try:
                    fut.result()
                except Exception as e:
                    log.error("listing_error", source=src.name, err=str(e))
    # Both pools are drained by the time the `with` blocks have unwound, so
    # every finalize future below is already resolved.
    for fut in final_futures:
        try:
            upserted, price_drop = fut.result()
        except Exception as e:
            log.error("listing_error", source=src.name, err=str(e))
            continue
        if upserted:
            sc["kept"] += 1
            sc["upserted"] += 1
            if price_drop is not None:
                sc["price_drop"] += 1
    return sc, seen


def _reconcile_alive(
    store: NotionStore,
    src_by_name: dict[str, Source],
    seen_ids: dict[str, set[str]],
    completed: set[str],
    kind: str,
    stats: dict,
) -> None:
    pmax = settings.buy_price_max if kind == "buy" else settings.rent_price_max
    pmin = settings.buy_price_min if kind == "buy" else settings.rent_price_min
    stats_lock = threading.Lock()

    def _job(alive: dict) -> None:
        src_name = alive["source"]
        if src_name not in completed:
            return
        if alive["external_id"] in seen_ids.get(src_name, set()):
            store.touch_alive(alive["page_id"])
            log.info("still_alive", id=f"{src_name}:{alive['external_id']}")
            return
        src = src_by_name.get(src_name)
        alive_flag, price = (False, None)
        if src is not None:
            alive_flag, price = src.recheck(
                external_id=alive["external_id"], url=alive["url"], kind=kind
            )
        if alive_flag and price is not None and price > pmax:
            store.mark_price_up(alive["page_id"], kind=kind, price=price)
            with stats_lock:
                stats["price_up"] += 1
            log.info("marked_price_up", id=f"{src_name}:{alive['external_id']}", price=price)
        elif alive_flag and price is not None and 0 < price < pmin:
            store.mark_price_down(
                alive["page_id"], kind=kind, price=price, prev_price=alive.get("price")
            )
            with stats_lock:
                stats["price_drop"] += 1
            log.info("marked_price_down", id=f"{src_name}:{alive['external_id']}", price=price)
        else:
            store.mark_gone(alive["page_id"])
            with stats_lock:
                stats["gone"] += 1
            log.info("marked_gone", id=f"{src_name}:{alive['external_id']}")

    with ThreadPoolExecutor(max_workers=settings.pool_workers) as pool:
        futures = [pool.submit(_job, alive) for alive in store.list_alive(kind=kind)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log.error("reconcile_error", kind=kind, err=str(e))


def run_rent_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    store.ensure_columns("rent")
    # One sweep now instead of a lookup query per listing later, all of
    # which would queue behind the Notion pacer.
    store.prime_index("rent")
    sources = enabled_sources()
    today = date.today()
    stats = {
        "fetched": 0,
        "prefiltered": 0,
        "kept": 0,
        "upserted": 0,
        "gone": 0,
        "price_up": 0,
        "price_drop": 0,
        "per_source": {},
    }

    seen_ids: dict[str, set[str]] = {s.name: set() for s in sources}
    completed: set[str] = set()

    for src in sources:
        try:
            sc, seen = _run_source(src, store, work, today, kind="rent")
            seen_ids[src.name] = seen
            stats["fetched"] += sc["fetched"]
            stats["prefiltered"] += sc["prefiltered"]
            stats["kept"] += sc["kept"]
            stats["upserted"] += sc["upserted"]
            stats["price_drop"] += sc["price_drop"]
            completed.add(src.name)
        except Exception as e:
            sc = {
                "fetched": 0,
                "prefiltered": 0,
                "kept": 0,
                "upserted": 0,
                "price_drop": 0,
            }
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    _reconcile_alive(store, src_by_name, seen_ids, completed, "rent", stats)
    return stats


def run_buy_cycle() -> dict:
    work = work_coords()
    store = NotionStore()
    store.ensure_columns("buy")
    # One sweep now instead of a lookup query per listing later, all of
    # which would queue behind the Notion pacer.
    store.prime_index("buy")
    sources = enabled_sources(mode="buy")
    today = date.today()
    reset_run_budget()
    stats = {
        "fetched": 0,
        "prefiltered": 0,
        "kept": 0,
        "upserted": 0,
        "gone": 0,
        "price_up": 0,
        "price_drop": 0,
        "per_source": {},
    }

    seen_ids: dict[str, set[str]] = {s.name: set() for s in sources}
    completed: set[str] = set()

    for src in sources:
        try:
            sc, seen = _run_source(src, store, work, today, kind="buy")
            seen_ids[src.name] = seen
            stats["fetched"] += sc["fetched"]
            stats["prefiltered"] += sc["prefiltered"]
            stats["kept"] += sc["kept"]
            stats["upserted"] += sc["upserted"]
            stats["price_drop"] += sc["price_drop"]
            completed.add(src.name)
        except Exception as e:
            sc = {
                "fetched": 0,
                "prefiltered": 0,
                "kept": 0,
                "upserted": 0,
                "price_drop": 0,
            }
            log.error("source_error", source=src.name, err=str(e))
        stats["per_source"][src.name] = sc
        log.info("source_done", source=src.name, **sc)

    src_by_name = {s.name: s for s in sources}
    _reconcile_alive(store, src_by_name, seen_ids, completed, "buy", stats)
    if settings.condo_enrich_enabled:
        stats["condo"] = registry().stats()
    return stats


def backfill_condos(
    kind: str = "buy",
    max_searches: int | None = None,
    neighborhoods: list[str] | None = None,
    bedrooms_min: int = 0,
    only_missing: bool = False,
    refresh: bool = False,
) -> dict:
    """Fill condo columns on existing Notion rows.

    Rows written before the CondoKey column existed have no key and are skipped;
    they self-heal on the next cycle. Not called from the run cycles.

    `neighborhoods` / `bedrooms_min` / `only_missing` narrow the sweep. Resolving
    every building in the base is ~7 hours of agent time, while the shortlist
    that actually gets read is a few dozen rows — do that one first and measure.
    """
    wanted = {n.strip().lower() for n in (neighborhoods or []) if n.strip()}
    if max_searches is not None:
        settings.condo_search_max_per_run = max_searches
    store = NotionStore()
    store.ensure_columns(kind)
    reset_run_budget()
    from .condo_agent import reset_rate_limit

    # The account's limit resets on its own clock; a new run gets a fresh try.
    reset_rate_limit()
    stats = {"examined": 0, "skipped": 0, "named": 0, "dated": 0, "updated": 0}
    stats_lock = threading.Lock()

    def _job(row: dict) -> None:
        with stats_lock:
            stats["examined"] += 1
        if (
            not row.get("condo_key")
            or (wanted and (row.get("neighborhood") or "").lower() not in wanted)
            or (bedrooms_min and int(row.get("bedrooms") or 0) < bedrooms_min)
            or (only_missing and row.get("built_year"))
        ):
            with stats_lock:
                stats["skipped"] += 1
            return
        # The unit's own numbers ride along: with no building name, they are
        # what tells two condominiums on the same street apart.
        lst = Listing(
            source=row["source"],
            external_id=row["external_id"],
            url=row["url"],
            neighborhood=row.get("neighborhood") or "",
            price_rent=0.0,
            price_total=0.0,
            bedrooms=int(row.get("bedrooms") or 0),
            bathrooms=0,
            suites=int(row.get("suites") or 0),
            parking=int(row.get("parking") or 0),
            sqm=float(row.get("sqm") or 0),
            pets=False,
            furnished=None,
            lat=None,
            lng=None,
            mode=kind,
            condo_fee=float(row.get("condo_fee") or 0),
            condo_key=row["condo_key"],
            condo_name=row.get("condo_name") or "",
            condo_confidence=row.get("condo_confidence") or "",
        )
        year = row.get("built_year")
        # Seeding the stored year makes resolve_condo return early, which is what
        # we want on a normal pass. After a `condo-invalidate` it is exactly
        # wrong: the row's year is the value being retired, and seeding it would
        # simply re-stamp it. `refresh` drops it so the resolver decides again.
        if year and not refresh:
            lst.apply_built_year(int(year))
        resolve_condo(lst)
        if not (lst.condo_name or lst.built_year or lst.condo_confidence):
            return
        store.update_condo(row["page_id"], lst)
        with stats_lock:
            stats["updated"] += 1
            if lst.condo_name:
                stats["named"] += 1
            if lst.built_year:
                stats["dated"] += 1

    # Every job here is an agent lookup, which _search_budget caps at
    # condo_agent_concurrency anyway. A wider pool would only pile up threads
    # blocked on that semaphore.
    with ThreadPoolExecutor(max_workers=settings.condo_pool_size) as pool:
        futures = [pool.submit(_job, row) for row in store.list_alive(kind=kind)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log.error("condo_backfill_error", kind=kind, err=str(e))
    stats["registry"] = registry().stats()
    from .condo_agent import limit_reason, rate_limited

    if rate_limited():
        # Surface it in the command's own output: a run that stopped resolving
        # half way through is not the same as a run that found nothing.
        stats["rate_limited"] = limit_reason()
    return stats


def backfill_listed_at(kind: str = "rent", include_gone: bool = True) -> dict:
    """Stamp ListedAt on rows the normal cycle never rewrites.

    Only listings that come back in search results get re-upserted, so gone /
    price-up / price-drop rows keep counting availability from FirstSeen unless
    they are backfilled here. Rows that already carry a date are skipped.
    """
    store = NotionStore()
    store.ensure_columns(kind)
    sources = enabled_sources(mode=kind)
    src_by_name = {s.name: s for s in sources}
    stats = {"examined": 0, "skipped": 0, "no_source": 0, "no_date": 0, "updated": 0}
    stats_lock = threading.Lock()

    def _job(row: dict) -> None:
        with stats_lock:
            stats["examined"] += 1
        if row.get("listed_at"):
            with stats_lock:
                stats["skipped"] += 1
            return
        src = src_by_name.get(row["source"])
        if src is None:
            with stats_lock:
                stats["no_source"] += 1
            return
        d = src.fetch_listed_at(
            external_id=row["external_id"], url=row["url"], kind=kind
        )
        if not d:
            with stats_lock:
                stats["no_date"] += 1
            return
        store.update_listed_at(row["page_id"], d)
        with stats_lock:
            stats["updated"] += 1
        log.info(
            "listed_at_backfilled",
            id=f"{row['source']}:{row['external_id']}",
            listed_at=d.isoformat(),
        )

    rows = list(store.list_alive(kind=kind))
    if include_gone:
        rows += list(store.list_gone(kind=kind))
    with ThreadPoolExecutor(max_workers=settings.pool_workers) as pool:
        futures = [pool.submit(_job, row) for row in rows]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log.error("listed_at_backfill_error", kind=kind, err=str(e))
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
    pmin = settings.buy_price_min if kind == "buy" else settings.rent_price_min
    stats = {"examined": 0, "price_up": 0, "price_drop": 0, "skipped": 0}

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
        elif alive_flag and price is not None and 0 < price < pmin:
            store.mark_price_down(
                gone["page_id"], kind=kind, price=price, prev_price=gone.get("price")
            )
            stats["price_drop"] += 1
            log.info("recheck_pricedown", id=f"{src_name}:{gone['external_id']}", price=price)
        else:
            stats["skipped"] += 1

    return stats
