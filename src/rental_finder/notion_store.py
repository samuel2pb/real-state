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


def _retry(fn, *a, **kw):
    base = settings.http_backoff_base_sec
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            return fn(*a, **kw)
        except APIResponseError as e:
            status = getattr(e, "status", 0)
            if status == 429 or 500 <= status < 600:
                wait = base * (2 ** (attempt - 1))
                log.warning("notion_backoff", status=status, wait=wait, attempt=attempt)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("notion_retry_exhausted")


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
        self._lock = threading.Lock()

    def _db_id(self, kind: str) -> str:
        if kind == "rent":
            if not settings.notion_rent_db_id:
                raise RuntimeError(
                    "NOTION_RENT_DB_ID unset; run scripts/setup_notion.py"
                )
            return settings.notion_rent_db_id
        if kind == "buy":
            if not settings.notion_buy_db_id:
                raise RuntimeError(
                    "NOTION_BUY_DB_ID unset; run scripts/setup_notion.py --kind buy"
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
        "CondoName": {"rich_text": {}},
        "CondoKey": {"rich_text": {}},
        "BuiltYear": {"number": {"format": "number"}},
        "CondoAge": {"number": {"format": "number"}},
        "CondoSource": {"select": {}},
    }

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

    def _find_page(self, kind: str, external_id: str, source: str) -> dict | None:
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
                "Pets": {"checkbox": lst.pets},
                "PropertyType": {"select": {"name": lst.property_type}}
                if lst.property_type
                else {"select": None},
                "Address": {
                    "rich_text": [{"text": {"content": (lst.address or "")[:1800]}}]
                },
                "LastSeen": {"date": {"start": lst.last_seen.isoformat()}},
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
            "Pets": {"checkbox": lst.pets},
            "PropertyType": {"select": {"name": lst.property_type}}
            if lst.property_type
            else {"select": None},
            "Address": {
                "rich_text": [{"text": {"content": (lst.address or "")[:1800]}}]
            },
            "LastSeen": {"date": {"start": lst.last_seen.isoformat()}},
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
        log.info("notion_create", id=lst.global_id, page=r["id"])
        return r["id"]

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

    def update_condo(self, page_id: str, lst: Listing) -> None:
        """Patch only the condo columns on an existing page (backfill path)."""
        props = self._strip_missing(_condo_props(lst))
        if not props:
            return
        _retry(self.client.pages.update, page_id=page_id, properties=props)

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
        cursor: str | None = None
        while True:
            kw: dict = {
                "data_source_id": ds_id,
                "filter": status_filter,
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
                    "url": p.get("URL", {}).get("url", ""),
                    "neighborhood": (p.get("Neighborhood", {}).get("select") or {}).get(
                        "name", ""
                    ),
                    "price": _stored_price(page, kind),
                    "condo_key": _plain(p.get("CondoKey")),
                    "condo_name": _plain(p.get("CondoName")),
                    "built_year": (p.get("BuiltYear") or {}).get("number"),
                }
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")

    def list_alive(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["available", "price-up", "price-drop"])

    def list_gone(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["gone"])


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
    }


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
