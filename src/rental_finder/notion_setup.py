"""Create the Notion database for a module (rent / buy) inside the parent page.

Used by `scripts/setup_notion.py` and by `rental-finder init`. The neighborhood
options come from the configured search, not from a hard-coded list, so a new
user's database starts out with their own bairros.
"""

from __future__ import annotations

import json

from notion_client import Client

from .config import ROOT, settings
from .notion_store import NotionStore

KINDS = ("rent", "buy")

# Notion select colors, cycled over the configured neighborhoods.
PALETTE = (
    "blue",
    "purple",
    "pink",
    "brown",
    "orange",
    "yellow",
    "green",
    "red",
    "gray",
)


def neighborhood_options(names: list[str]) -> list[dict]:
    """Select options for the configured neighborhoods, colors cycling PALETTE."""
    seen: set[str] = set()
    out: list[dict] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "color": PALETTE[len(out) % len(PALETTE)]})
    return out


def _default_neighborhoods(kind: str) -> list[str]:
    return settings.buy_neighborhoods_list if kind == "buy" else settings.neighborhoods_list


def title_for(kind: str, city: str | None = None) -> str:
    city = city or (settings.buy_city if kind == "buy" else settings.rent_city)
    return f"{kind.capitalize()} Listings — {city}"


def schema(kind: str = "rent", neighborhoods: list[str] | None = None) -> dict:
    if neighborhoods is None:
        neighborhoods = _default_neighborhoods(kind)
    shared = {
        "Name": {"title": {}},
        "URL": {"url": {}},
        "Source": {
            "select": {
                "options": [
                    {"name": "quintoandar", "color": "yellow"},
                ]
            }
        },
        "ExternalID": {"rich_text": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "available", "color": "green"},
                    {"name": "gone", "color": "red"},
                    {"name": "price-up", "color": "orange"},
                    {"name": "price-drop", "color": "blue"},
                ]
            }
        },
        "Neighborhood": {"select": {"options": neighborhood_options(neighborhoods)}},
        "Bedrooms": {"number": {"format": "number"}},
        "Bathrooms": {"number": {"format": "number"}},
        "Suites": {"number": {"format": "number"}},
        "Parking": {"number": {"format": "number"}},
        "Sqm": {"number": {"format": "number"}},
        "Pets": {"checkbox": {}},
        "PropertyType": {
            "select": {
                "options": [
                    {"name": "Apartamento", "color": "blue"},
                    {"name": "CasaCondominio", "color": "green"},
                    {"name": "Casa", "color": "brown"},
                ]
            }
        },
        "DistanceKm": {"number": {"format": "number_with_commas"}},
        "Address": {"rich_text": {}},
        "FirstSeen": {"date": {}},
        "LastSeen": {"date": {}},
        "PriceUp": {"number": {"format": "real"}},
        "PrevPrice": {"number": {"format": "real"}},
        "PriceDrop": {"number": {"format": "real"}},
        "PriceDropPct": {"number": {"format": "percent"}},
        "PriceChangedAt": {"date": {}},
        "ListedAt": {"date": {}},
        "AvailabilityDays": {
            "formula": {"expression": NotionStore.AVAILABILITY_DAYS_FORMULA}
        },
    }
    if kind == "buy":
        shared["Price"] = {"number": {"format": "real"}}
        shared["CondoFee"] = {"number": {"format": "real"}}
        shared["Iptu"] = {"number": {"format": "real"}}
    else:
        shared["PriceRent"] = {"number": {"format": "real"}}
        shared["PriceTotal"] = {"number": {"format": "real"}}
    return shared


def create_database(
    kind: str,
    *,
    token: str | None = None,
    parent_page_id: str | None = None,
    neighborhoods: list[str] | None = None,
    city: str | None = None,
) -> dict:
    """Create the `kind` database under the parent page.

    Returns {"id", "url", "kind", "properties_applied"}. The keyword overrides
    exist for `rental-finder init`, which writes a new .env after `settings`
    was already loaded from the old (or missing) one.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; must be one of {list(KINDS)}")
    token = token or settings.notion_token
    parent_page_id = parent_page_id or settings.notion_parent_page_id
    if not token or not parent_page_id:
        raise RuntimeError(
            "NOTION_TOKEN and NOTION_PARENT_PAGE_ID are required; run `rental-finder init`"
        )
    props = schema(kind, neighborhoods)
    c = Client(auth=token)
    db = c.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": title_for(kind, city)}}],
        initial_data_source={"properties": props},
    )
    ds_id = db["data_sources"][0]["id"] if db.get("data_sources") else None
    props_created = False
    if ds_id:
        ds = c.data_sources.retrieve(data_source_id=ds_id)
        props_created = len(ds.get("properties", {})) > 1
        if not props_created:
            c.data_sources.update(data_source_id=ds_id, properties=props)
            props_created = True
    out = {
        "kind": kind,
        "id": db["id"],
        "url": db.get("url"),
        "properties_applied": props_created,
    }
    cache = ROOT / ".cache" / f"notion_{kind}_db.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"id": out["id"], "url": out["url"]}, indent=2))
    return out
