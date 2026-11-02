from __future__ import annotations

import threading
import time
from datetime import date
from typing import Iterator

import structlog
from notion_client import Client
from notion_client.errors import APIResponseError

from .config import settings
from .models import Listing

log = structlog.get_logger(__name__)


# Notion allows ~3 requests/second per integration, averaged. The pipeline
# writes from a 10-thread pool, so without a pacer a backfill of a few hundred
# rows spends its whole retry budget on 429s and still drops ~10 rows per pass.
_PACE_LOCK = threading.Lock()
_last_call = 0.0


def _pace() -> None:
    global _last_call
    gap = 1.0 / max(settings.notion_max_rps, 0.1)
    with _PACE_LOCK:
        wait = _last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _retry(fn, *a, **kw):
    base = settings.http_backoff_base_sec
    last = 0
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            _pace()
            return fn(*a, **kw)
        except APIResponseError as e:
            status = getattr(e, "status", 0)
            if status == 429 or 500 <= status < 600:
                last = status
                # Honour Retry-After when Notion sends one; it knows better than
                # our doubling does.
                hdrs = getattr(getattr(e, "response", None), "headers", None) or {}
                try:
                    wait = float(hdrs.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                wait = wait or base * (2 ** (attempt - 1))
                log.warning("notion_backoff", status=status, wait=wait, attempt=attempt)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"notion_retry_exhausted status={last}")


def _stored_price(page: dict, kind: str) -> float | None:
    prop_name = "Price" if kind == "buy" else "PriceTotal"
    price = (page.get("properties") or {}).get(prop_name, {}).get("number")
    return price if price else None


class NotionStore:
    def __init__(self) -> None:
        self.client = Client(auth=settings.notion_token)
        self._ds_cache: dict[str, str] = {}
        self._missing_props: set[str] = set()
        self._schema_ensured: set[str] = set()
        # kind -> {(source, external_id): page}, primed by prime_index().
        self._index: dict[str, dict[tuple[str, str], dict]] = {}
        self._lock = threading.Lock()

    def _db_id(self, kind: str) -> str:
        if kind == "rent":
            if not settings.notion_rent_db_id:
                raise RuntimeError(
                    "NOTION_RENT_DB_ID unset; run `rental-finder init`"
                )
            return settings.notion_rent_db_id
        if kind == "buy":
            if not settings.notion_buy_db_id:
                raise RuntimeError(
                    "NOTION_BUY_DB_ID unset; run `rental-finder init`"
                )
            return settings.notion_buy_db_id
        raise NotImplementedError(f"db kind {kind!r} not implemented yet")

    def _data_source_id(self, kind: str) -> str:
        with self._lock:
            if kind in self._ds_cache:
                return self._ds_cache[kind]
        db = _retry(self.client.databases.retrieve, database_id=self._db_id(kind))
        sources = db.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"notion db for {kind!r} has no data_sources")
        ds_id = sources[0]["id"]
        with self._lock:
            self._ds_cache[kind] = ds_id
        return ds_id

    _COLUMN_SPEC = {
        "PriceUp": {"number": {"format": "real"}},
        "PrevPrice": {"number": {"format": "real"}},
        "PriceDrop": {"number": {"format": "real"}},
        "PriceDropPct": {"number": {"format": "percent"}},
        "PriceChangedAt": {"date": {}},
        "ListedAt": {"date": {}},
        "CondoName": {"rich_text": {}},
        "CondoKey": {"rich_text": {}},
        "BuiltYear": {"number": {"format": "number"}},
        "CondoAge": {"number": {"format": "number"}},
        "CondoSource": {"select": {}},
        "CondoConfidence": {"select": {}},
        "SqmTotal": {"number": {"format": "number"}},
        "SqmBasis": {"select": {}},
    }

    # Days on the market, counted from the ad's own publication date (ListedAt)
    # and only falling back to FirstSeen — when we first saw it — for sources
    # that publish no date. End is LastSeen so a gone listing freezes instead of
    # counting forever.
    AVAILABILITY_DAYS_FORMULA = """lets(
  start, if(empty(prop("ListedAt")), prop("FirstSeen"), prop("ListedAt")),
  finish, if(empty(prop("LastSeen")), now(), prop("LastSeen")),
  if(empty(start), 0, dateBetween(finish, start, "days"))
)"""

    _FORMULA_SPEC = {"AvailabilityDays": AVAILABILITY_DAYS_FORMULA}

    # list_alive filters on every one of these, and Notion 400s a query that
    # names a select option the property does not have.
    STATUS_OPTIONS = ("available", "gone", "price-up", "price-drop")

    def ensure_columns(self, kind: str = "rent") -> None:
        """Idempotently ensure analytics columns exist on the data source schema."""
        ds_id = self._data_source_id(kind)
        with self._lock:
            if ds_id in self._schema_ensured:
                return
        ds = _retry(self.client.data_sources.retrieve, data_source_id=ds_id)
        existing = ds.get("properties") or {}
        for col, spec in self._COLUMN_SPEC.items():
            if col not in existing:
                _retry(
                    self.client.data_sources.update,
                    data_source_id=ds_id,
                    properties={col: spec},
                )
                log.info("notion_col_added", kind=kind, col=col)
        self._ensure_status_options(ds_id, existing, kind)
        self._ensure_formulas(ds_id, existing, kind)
        with self._lock:
            self._schema_ensured.add(ds_id)

    def _ensure_status_options(self, ds_id: str, existing: dict, kind: str) -> None:
        """Add any missing Status select options, preserving the existing ones."""
        current = ((existing.get("Status") or {}).get("select") or {}).get("options")
        if current is None:
            return  # no Status property yet; setup_notion owns creating it
        have = {o.get("name") for o in current}
        missing = [s for s in self.STATUS_OPTIONS if s not in have]
        if not missing:
            return
        merged = [
            {k: v for k, v in o.items() if k in ("name", "color", "id")}
            for o in current
        ] + [{"name": s} for s in missing]
        _retry(
            self.client.data_sources.update,
            data_source_id=ds_id,
            properties={"Status": {"select": {"options": merged}}},
        )
        log.info("notion_status_options_added", kind=kind, added=missing)

    def _ensure_formulas(self, ds_id: str, existing: dict, kind: str) -> None:
        """Create or re-sync the formula columns this code owns."""
        for name, expression in self._FORMULA_SPEC.items():
            current = ((existing.get(name) or {}).get("formula") or {}).get(
                "expression"
            )
            if current == expression:
                continue
            _retry(
                self.client.data_sources.update,
                data_source_id=ds_id,
                properties={name: {"formula": {"expression": expression}}},
            )
            log.info(
                "notion_formula_synced",
                kind=kind,
                col=name,
                created=current is None,
            )

    def prime_index(self, kind: str) -> int:
        """Snapshot every row of this base as (source, external_id) -> page.

        upsert_listing otherwise burns one query per listing just to learn the
        row's page id, and each of those queries queues behind the 2.5 rps
        pacer. One sweep costs a query per 100 rows instead.

        The snapshot is installed only once the sweep has finished, so a run
        that fails half way leaves the index absent and _find_page falls back to
        querying. That ordering is what keeps a partial prime from reading as
        "row does not exist" and creating duplicates.
        """
        if not settings.notion_page_index:
            return 0
        ds_id = self._data_source_id(kind)
        index: dict[tuple[str, str], dict] = {}
        cursor: str | None = None
        queries = 0
        while True:
            kw: dict = {
                "data_source_id": ds_id,
                "page_size": settings.notion_index_page_size,
            }
            if cursor:
                kw["start_cursor"] = cursor
            r = _retry(self.client.data_sources.query, **kw)
            queries += 1
            for page in r.get("results") or []:
                props = page.get("properties") or {}
                ext = _plain(props.get("ExternalID"))
                src = (props.get("Source", {}).get("select") or {}).get("name", "")
                if ext:
                    index[(src, ext)] = page
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        with self._lock:
            self._index[kind] = index
        log.info("notion_index_primed", kind=kind, rows=len(index), queries=queries)
        return len(index)

    def _remember_page(self, kind: str, source: str, external_id: str, page: dict) -> None:
        """Register a row this run just created, so a later lookup still hits."""
        with self._lock:
            idx = self._index.get(kind)
            if idx is not None:
                idx[(source, external_id)] = page

    def _find_page(self, kind: str, external_id: str, source: str) -> dict | None:
        with self._lock:
            idx = self._index.get(kind)
        if idx is not None:
            # A primed index is a complete snapshot plus everything this run has
            # created, so a miss means the row genuinely is not there. Querying
            # to confirm would put back exactly the per-listing call the index
            # exists to remove.
            return idx.get((source, external_id))
        ds_id = self._data_source_id(kind)
        r = _retry(
            self.client.data_sources.query,
            data_source_id=ds_id,
            **{
                "filter": {
                    "and": [
                        {
                            "property": "ExternalID",
                            "rich_text": {"equals": external_id},
                        },
                        {"property": "Source", "select": {"equals": source}},
                    ]
                },
                "page_size": 1,
            },
        )
        results = r.get("results") or []
        return results[0] if results else None

    def _properties(self, lst: Listing) -> dict:
        if lst.mode == "buy":
            name = f"{lst.neighborhood} · {lst.bedrooms}BR · R$ {int(lst.price_sale):,}"
            props = {
                "Name": {"title": [{"text": {"content": name[:200]}}]},
                "URL": {"url": lst.url},
                "Source": {"select": {"name": lst.source}},
                "ExternalID": {"rich_text": [{"text": {"content": lst.external_id}}]},
                "Status": {"select": {"name": lst.status}},
                "Neighborhood": {"select": {"name": lst.neighborhood}}
                if lst.neighborhood
                else {"select": None},
                "Price": {"number": lst.price_sale},
                "PriceUp": {"number": None},
                "CondoFee": {"number": lst.condo_fee},
                "Iptu": {"number": lst.iptu},
                "Bedrooms": {"number": lst.bedrooms},
                "Bathrooms": {"number": lst.bathrooms},
                "Suites": {"number": lst.suites},
                "Parking": {"number": lst.parking},
                "Sqm": {"number": lst.sqm},
                **_area_props(lst),
                "Pets": {"checkbox": lst.pets},
                "PropertyType": {"select": {"name": lst.property_type}}
                if lst.property_type
                else {"select": None},
                "Address": {
                    "rich_text": [{"text": {"content": (lst.address or "")[:1800]}}]
                },
                "LastSeen": {"date": {"start": lst.last_seen.isoformat()}},
                **_listed_at_props(lst),
                **_condo_props(lst),
            }
            if lst.distance_km is not None:
                props["DistanceKm"] = {"number": round(lst.distance_km, 2)}
            return props
        # rent mode (default)
        name = f"{lst.neighborhood} · {lst.bedrooms}BR · R$ {int(lst.price_total)}"
        props = {
            "Name": {"title": [{"text": {"content": name[:200]}}]},
            "URL": {"url": lst.url},
            "Source": {"select": {"name": lst.source}},
            "ExternalID": {"rich_text": [{"text": {"content": lst.external_id}}]},
            "Status": {"select": {"name": lst.status}},
            "Neighborhood": {"select": {"name": lst.neighborhood}}
            if lst.neighborhood
            else {"select": None},
            "PriceRent": {"number": lst.price_rent},
            "PriceTotal": {"number": lst.price_total},
            "PriceUp": {"number": None},
            "Bedrooms": {"number": lst.bedrooms},
            "Bathrooms": {"number": lst.bathrooms},
            "Suites": {"number": lst.suites},
            "Parking": {"number": lst.parking},
            "Sqm": {"number": lst.sqm},
            **_area_props(lst),
            "Pets": {"checkbox": lst.pets},
            "PropertyType": {"select": {"name": lst.property_type}}
            if lst.property_type
            else {"select": None},
            "Address": {
                "rich_text": [{"text": {"content": (lst.address or "")[:1800]}}]
            },
            "LastSeen": {"date": {"start": lst.last_seen.isoformat()}},
            **_listed_at_props(lst),
        }
        if lst.distance_km is not None:
            props["DistanceKm"] = {"number": round(lst.distance_km, 2)}
        return props

    def _strip_missing(self, props: dict) -> dict:
        with self._lock:
            missing = set(self._missing_props)
        if missing:
            return {k: v for k, v in props.items() if k not in missing}
        return props

    def upsert_listing(self, lst: Listing, kind: str = "rent") -> str:
        ds_id = self._data_source_id(kind)
        page = self._find_page(kind, lst.external_id, lst.source)
        props = self._properties(lst)
        if page:
            page_id = page["id"]
            old = _stored_price(page, kind)
            new = lst.price_sale if kind == "buy" else lst.price_total
            if old and new and old > 0 and new > 0 and new < old:
                drop = old - new
                pct = drop / old * 100
                if pct >= settings.price_drop_min_pct:
                    props["PrevPrice"] = {"number": old}
                    props["PriceDrop"] = {"number": drop}
                    props["PriceDropPct"] = {"number": pct / 100}
                    props["PriceChangedAt"] = {"date": {"start": date.today().isoformat()}}
                    lst.prev_price = old
                    lst.price_drop = drop
                    lst.price_drop_pct = pct
                    log.info(
                        "price_drop",
                        id=lst.global_id,
                        prev=old,
                        new=new,
                        pct=round(pct, 2),
                    )
            elif old and new and new > old:
                props["PrevPrice"] = {"number": None}
                props["PriceDrop"] = {"number": None}
                props["PriceDropPct"] = {"number": None}
                props["PriceChangedAt"] = {"date": None}
            props = self._strip_missing(props)
            try:
                _retry(self.client.pages.update, page_id=page_id, properties=props)
            except APIResponseError as e:
                prop = _extract_missing_prop(e)
                if prop:
                    with self._lock:
                        self._missing_props.add(prop)
                    log.warning("notion_prop_missing", prop=prop)
                    props = self._strip_missing(props)
                    _retry(self.client.pages.update, page_id=page_id, properties=props)
                else:
                    raise
            log.info("notion_update", id=lst.global_id)
            return page_id
        props = self._strip_missing(props)
        props["FirstSeen"] = {"date": {"start": lst.first_seen.isoformat()}}
        try:
            r = _retry(
                self.client.pages.create,
                parent={"type": "data_source_id", "data_source_id": ds_id},
                properties=props,
            )
        except APIResponseError as e:
            prop = _extract_missing_prop(e)
            if prop:
                self._missing_props.add(prop)
                log.warning("notion_prop_missing", prop=prop)
                props = self._strip_missing(props)
                r = _retry(
                    self.client.pages.create,
                    parent={"type": "data_source_id", "data_source_id": ds_id},
                    properties=props,
                )
            else:
                raise
        self._remember_page(kind, lst.source, lst.external_id, r)
        log.info("notion_create", id=lst.global_id, page=r["id"])
        return r["id"]

    def archive_page(self, page_id: str) -> None:
        """Move a row to Notion's trash.

        Trash, not a hard delete: the row is recoverable for 30 days, which is
        the difference between a junk filter and losing data to a wrong year.
        """
        _retry(self.client.pages.update, page_id=page_id, in_trash=True)

    def _forget_page(self, kind: str, source: str, external_id: str) -> None:
        """Drop a row from the primed index after it left the base.

        Without this the snapshot still hands the trashed page back to
        _find_page and the next upsert patches an archived row, which Notion
        rejects.
        """
        with self._lock:
            idx = self._index.get(kind)
            if idx is not None:
                idx.pop((source, external_id), None)

    def drop_listing(self, lst: Listing, kind: str = "rent") -> bool:
        """Trash this listing's row if it has one. True when a row was removed."""
        page = self._find_page(kind, lst.external_id, lst.source)
        if not page:
            return False
        self.archive_page(page["id"])
        self._forget_page(kind, lst.source, lst.external_id)
        log.info("notion_archived", id=lst.global_id, page=page["id"])
        return True

    def mark_gone(self, page_id: str) -> None:
        _retry(
            self.client.pages.update,
            page_id=page_id,
            properties={
                "Status": {"select": {"name": "gone"}},
            },
        )

    def touch_alive(self, page_id: str) -> None:
        _retry(
            self.client.pages.update,
            page_id=page_id,
            properties={
                "LastSeen": {"date": {"start": date.today().isoformat()}},
            },
        )

    def mark_price_up(self, page_id: str, kind: str = "rent", price: float | None = None) -> None:
        props: dict = {
            "Status": {"select": {"name": "price-up"}},
            "LastSeen": {"date": {"start": date.today().isoformat()}},
        }
        if price is not None:
            props["PriceUp"] = {"number": price}
        _retry(self.client.pages.update, page_id=page_id, properties=props)

    def mark_price_down(
        self,
        page_id: str,
        kind: str = "rent",
        price: float | None = None,
        prev_price: float | None = None,
    ) -> None:
        props: dict = {
            "Status": {"select": {"name": "price-drop"}},
            "LastSeen": {"date": {"start": date.today().isoformat()}},
        }
        if price is not None and prev_price is not None and prev_price > price > 0:
            props["PrevPrice"] = {"number": prev_price}
            props["PriceDrop"] = {"number": prev_price - price}
            props["PriceDropPct"] = {"number": (prev_price - price) / prev_price}
            props["PriceChangedAt"] = {"date": {"start": date.today().isoformat()}}
        _retry(self.client.pages.update, page_id=page_id, properties=props)

    def update_listed_at(self, page_id: str, listed_at: date) -> None:
        """Patch only ListedAt on an existing page (backfill path)."""
        _retry(
            self.client.pages.update,
            page_id=page_id,
            properties={"ListedAt": {"date": {"start": listed_at.isoformat()}}},
        )

    def update_condo(self, page_id: str, lst: Listing) -> None:
        """Patch only the condo columns on an existing page (backfill path)."""
        props = self._strip_missing(_condo_props(lst))
        if not props:
            return
        _retry(self.client.pages.update, page_id=page_id, properties=props)

    def duplicate_groups(self, kind: str) -> list[list[dict]]:
        """Every (source, external_id) held by more than one row in this base.

        Each row: page_id, source, external_id, status, first_seen, last_seen,
        created (Notion created_time). Dates are ISO strings or "".
        """
        ds_id = self._data_source_id(kind)
        groups: dict[tuple[str, str], list[dict]] = {}
        cursor: str | None = None
        while True:
            kw: dict = {"data_source_id": ds_id, "page_size": 100}
            if cursor:
                kw["start_cursor"] = cursor
            r = _retry(self.client.data_sources.query, **kw)
            for page in r.get("results") or []:
                p = page.get("properties") or {}
                ext = _plain(p.get("ExternalID"))
                if not ext:
                    continue
                src = (p.get("Source", {}).get("select") or {}).get("name", "")
                groups.setdefault((src, ext), []).append(
                    {
                        "page_id": page["id"],
                        "source": src,
                        "external_id": ext,
                        "status": ((p.get("Status") or {}).get("select") or {}).get(
                            "name", ""
                        ),
                        "first_seen": _date_start(p.get("FirstSeen")),
                        "last_seen": _date_start(p.get("LastSeen")),
                        "created": page.get("created_time") or "",
                    }
                )
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        return [rows for rows in groups.values() if len(rows) > 1]

    def set_first_seen(self, page_id: str, iso: str) -> None:
        _retry(
            self.client.pages.update,
            page_id=page_id,
            properties={"FirstSeen": {"date": {"start": iso}}},
        )

    def _list_by_status(self, kind: str, statuses: list[str]) -> Iterator[dict]:
        ds_id = self._data_source_id(kind)
        if len(statuses) == 1:
            status_filter: dict = {
                "property": "Status",
                "select": {"equals": statuses[0]},
            }
        else:
            status_filter = {
                "or": [
                    {"property": "Status", "select": {"equals": s}} for s in statuses
                ]
            }
        yield from self._query_rows(kind, ds_id, status_filter)

    def list_by_source(self, kind: str, source: str) -> Iterator[dict]:
        """Every row of the base whose Source is `source`, whatever its Status.

        A base that never had that option 400s the filter; that just means
        there is nothing to list.
        """
        ds_id = self._data_source_id(kind)
        try:
            yield from self._query_rows(
                kind, ds_id, {"property": "Source", "select": {"equals": source}}
            )
        except APIResponseError as e:
            if getattr(e, "status", 0) != 400:
                raise
            log.warning("notion_source_filter_rejected", kind=kind, source=source, err=str(e))

    def _query_rows(self, kind: str, ds_id: str, row_filter: dict) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            kw: dict = {
                "data_source_id": ds_id,
                "filter": row_filter,
                "page_size": 100,
            }
            if cursor:
                kw["start_cursor"] = cursor
            r = _retry(self.client.data_sources.query, **kw)
            for page in r.get("results") or []:
                p = page["properties"]
                yield {
                    "page_id": page["id"],
                    "external_id": _plain(p.get("ExternalID")),
                    "source": (p.get("Source", {}).get("select") or {}).get("name", ""),
                    "status": (p.get("Status", {}).get("select") or {}).get("name", ""),
                    "url": p.get("URL", {}).get("url", ""),
                    "neighborhood": (p.get("Neighborhood", {}).get("select") or {}).get(
                        "name", ""
                    ),
                    "price": _stored_price(page, kind),
                    "condo_key": _plain(p.get("CondoKey")),
                    "condo_name": _plain(p.get("CondoName")),
                    "built_year": (p.get("BuiltYear") or {}).get("number"),
                    "condo_confidence": (
                        (p.get("CondoConfidence") or {}).get("select") or {}
                    ).get("name", ""),
                    "listed_at": ((p.get("ListedAt") or {}).get("date") or {}).get(
                        "start"
                    ),
                    # Enough of the unit to tell two condos on one street apart
                    # when the listing carries no building name.
                    "sqm": (p.get("Sqm") or {}).get("number") or 0,
                    "condo_fee": (p.get("CondoFee") or {}).get("number") or 0,
                    "bedrooms": (p.get("Bedrooms") or {}).get("number") or 0,
                    "suites": (p.get("Suites") or {}).get("number") or 0,
                    "parking": (p.get("Parking") or {}).get("number") or 0,
                    "address": _plain(p.get("Address")),
                }
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")

    def list_alive(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["available", "price-up", "price-drop"])

    def list_gone(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["gone"])


def _listed_at_props(lst: Listing) -> dict:
    """ListedAt only when the source told us. Absent means 'leave the cell alone'."""
    if not lst.listed_at:
        return {}
    return {"ListedAt": {"date": {"start": lst.listed_at.isoformat()}}}


def _area_props(lst: Listing) -> dict:
    """What `Sqm` actually measures, and the total when the source splits them.

    QuintoAndar publishes one unlabelled `area`, so its rows say "unspecified"
    rather than pretending the number is private area. Any R$/m2 built on an
    "unspecified" row is a floor, not a value: a total-area number understates
    the real price per square metre.
    """
    return {
        "SqmTotal": {"number": lst.sqm_total or None},
        "SqmBasis": {"select": {"name": lst.sqm_basis}}
        if lst.sqm_basis
        else {"select": None},
    }


def _condo_props(lst: Listing) -> dict:
    """Condo columns, null-safe. Empty values clear the cell rather than lie."""
    return {
        "CondoName": {
            "rich_text": [{"text": {"content": lst.condo_name[:1800]}}]
            if lst.condo_name
            else []
        },
        "CondoKey": {
            "rich_text": [{"text": {"content": lst.condo_key}}]
            if lst.condo_key
            else []
        },
        "BuiltYear": {"number": lst.built_year},
        "CondoAge": {"number": lst.condo_age_years},
        "CondoSource": {"select": {"name": lst.condo_source}}
        if lst.condo_source
        else {"select": None},
        # Written on every enriched row, including misses ("not_found"), so a
        # Ranking formula can require high/medium before it trusts CondoAge.
        "CondoConfidence": {"select": {"name": lst.condo_confidence}}
        if lst.condo_confidence
        else {"select": None},
    }


def _date_start(prop: dict | None) -> str:
    return ((prop or {}).get("date") or {}).get("start") or ""


def _plain(rt_prop: dict | None) -> str:
    if not rt_prop:
        return ""
    items = rt_prop.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items)


def _extract_missing_prop(e: APIResponseError) -> str | None:
    """Return the property name from a 'X is not a property that exists' error."""
    import re

    if getattr(e, "status", 0) != 400:
        return None
    msg = str(e)
    m = re.search(r"(\w+) is not a property that exists", msg)
    return m.group(1) if m else None
