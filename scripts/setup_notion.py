"""One-time: create a Notion DB per module inside the parent page.

Rent DB already created on initial setup — this script is here for future
buy / sell / rent_for_others modules to re-use the same schema approach.

Usage:
    uv run python scripts/setup_notion.py --kind rent
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from notion_client import Client

from rental_finder.config import settings

ROOT = Path(__file__).resolve().parents[1]

NEIGHBORHOOD_OPTIONS = [
    {"name": "Moema", "color": "blue"},
    {"name": "Jardim Paulistano", "color": "purple"},
    {"name": "Jardim Paulista", "color": "pink"},
    {"name": "Vila Nova Conceição", "color": "brown"},
    {"name": "Campo Belo", "color": "orange"},
    {"name": "Brooklin", "color": "yellow"},
    {"name": "Itaim Bibi", "color": "green"},
    {"name": "Vila Olímpia", "color": "red"},
    {"name": "Pinheiros", "color": "gray"},
]

TITLES = {
    "rent": "Rent Listings — São Paulo",
    "buy": "Buy Listings — São Paulo",
    "sell": "Sell Listings — São Paulo",
    "rent_for_others": "Rent-for-Others Listings — São Paulo",
}


def _schema(kind: str = "rent") -> dict:
    shared = {
        "Name": {"title": {}},
        "URL": {"url": {}},
        "Source": {
            "select": {
                "options": [
                    {"name": "zap", "color": "orange"},
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
                ]
            }
        },
        "Neighborhood": {"select": {"options": NEIGHBORHOOD_OPTIONS}},
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
    }
    if kind == "buy":
        shared["Price"] = {"number": {"format": "real"}}
        shared["CondoFee"] = {"number": {"format": "real"}}
        shared["Iptu"] = {"number": {"format": "real"}}
    else:
        shared["PriceRent"] = {"number": {"format": "real"}}
        shared["PriceTotal"] = {"number": {"format": "real"}}
    return shared


def main(kind: str = "rent") -> None:
    if kind not in TITLES:
        raise typer.BadParameter(
            f"unknown kind {kind!r}; must be one of {list(TITLES)}"
        )
    c = Client(auth=settings.notion_token)
    db = c.databases.create(
        parent={"type": "page_id", "page_id": settings.notion_parent_page_id},
        title=[{"type": "text", "text": {"content": TITLES[kind]}}],
        initial_data_source={"properties": _schema(kind)},
    )
    ds_id = db["data_sources"][0]["id"] if db.get("data_sources") else None
    props_created = False
    if ds_id:
        ds = c.data_sources.retrieve(data_source_id=ds_id)
        props_created = len(ds.get("properties", {})) > 1
        if not props_created:
            c.data_sources.update(data_source_id=ds_id, properties=_schema(kind))
            props_created = True
    typer.echo(f"Created {kind} DB: {db['id']}")
    typer.echo(f"URL: {db.get('url')}")
    typer.echo(f"Properties applied: {props_created}")
    typer.echo(f"-> paste into .env as NOTION_{kind.upper()}_DB_ID")
    (ROOT / f".cache/notion_{kind}_db.json").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / f".cache/notion_{kind}_db.json").write_text(
        json.dumps({"id": db["id"], "url": db.get("url")}, indent=2)
    )


if __name__ == "__main__":
    typer.run(main)
