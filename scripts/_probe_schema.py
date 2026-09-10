"""Dump the Notion data-source schema for a module (debug helper)."""
from __future__ import annotations

import json
import sys

from notion_client import Client

from rental_finder.config import settings

kind = sys.argv[1] if len(sys.argv) > 1 else "rent"
db_id = settings.notion_rent_db_id if kind == "rent" else settings.notion_buy_db_id
c = Client(auth=settings.notion_token)
db = c.databases.retrieve(database_id=db_id)
ds_id = db["data_sources"][0]["id"]
ds = c.data_sources.retrieve(data_source_id=ds_id)
for name, spec in (ds.get("properties") or {}).items():
    t = spec.get("type")
    extra = ""
    if t == "formula":
        extra = " := " + json.dumps(spec.get("formula", {}).get("expression"))
    print(f"{name:20s} {t}{extra}")
