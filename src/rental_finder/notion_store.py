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

    def ensure_columns(self, kind: str = "rent") -> None:
        """Idempotently ensure analytics columns exist on the data source schema."""
        ds_id = self._data_source_id(kind)
        with self._lock:
            if ds_id in self._schema_ensured:
                return
        ds = _retry(self.client.data_sources.retrieve, data_source_id=ds_id)
        existing = ds.get("properties") or {}
        if "PriceUp" not in existing:
            _retry(
                self.client.data_sources.update,
                data_source_id=ds_id,
                properties={"PriceUp": {"number": {"format": "real"}}},
            )
            log.info("notion_col_added", kind=kind, col="PriceUp")
        with self._lock:
            self._schema_ensured.add(ds_id)

    def _find(self, kind: str, external_id: str, source: str) -> str | None:
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
        return results[0]["id"] if results else None

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
        page_id = self._find(kind, lst.external_id, lst.source)
        props = self._strip_missing(self._properties(lst))
        if page_id:
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
                }
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")

    def list_alive(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["available", "price-up"])

    def list_gone(self, kind: str = "rent") -> Iterator[dict]:
        yield from self._list_by_status(kind, ["gone"])


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
