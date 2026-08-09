# rental-finder

Scrapes São Paulo rental listings from **zapimoveis.com.br** and **quintoandar.com.br**, filters by your criteria, and pushes them to a Notion database. Runs N times a day via cron.

Future modules: buy, sell, rent-for-others (same `Source` abstraction).

---

## Setup

```bash
uv sync
cp .env.example .env   # then edit .env (NOTION_TOKEN etc.)
```

**Notion integration one-time:**
1. Create an internal integration at https://www.notion.so/profile/integrations
2. Copy the `ntn_...` secret → `NOTION_TOKEN` in `.env`
3. Open your parent page in Notion → `...` menu → **Connections** → add the integration
4. The rent DB has already been created (id in `.env` as `NOTION_RENT_DB_ID`). For additional modules: `uv run python scripts/setup_notion.py --kind buy`

---

## Commands

```bash
uv run rental-finder geo         # warm geocoding cache (runs once, ~15s)
uv run rental-finder run-once    # one full fetch → filter → upsert cycle
uv run rental-finder schedule    # start the cron scheduler (blocking)

uv run rental-finder condo-stats                       # what the condo registry knows
uv run rental-finder condo-backfill --kind buy --max 20  # fill condo cols on existing rows
uv run rental-finder condo-search Mustique "Rua Arandu" 222  # debug one year lookup
uv run rental-finder condo-retry-misses                # reopen parked buildings
```

Run `schedule` inside a VPN session. The HTTP stack uses `curl-cffi` w/ Chrome TLS impersonation + exponential backoff on 403/429 — tolerant to IP changes.

---

## Configuration reference

All params live in `.env`. Key groups:

| Group | Vars |
|-------|------|
| Notion | `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID`, `NOTION_RENT_DB_ID` |
| Filters (rent) | `RENT_NEIGHBORHOODS`, `RENT_PRICE_MIN/MAX`, `RENT_BEDROOMS_MIN/MAX`, `RENT_BATHROOMS_MIN`, `RENT_SUITES_MIN`, `RENT_SQM_MIN`, `RENT_PARKING_MIN`, `RENT_PETS_REQUIRED` |
| Work location | `WORK_ADDRESS`, `WORK_MAX_DISTANCE_KM`, `WORK_LAT`, `WORK_LNG` (optional) |
| Schedule | `SCHEDULE_CRON_TIMES` (comma-separated cron exprs), `SCHEDULE_TZ` |
| HTTP | `HTTP_IMPERSONATE`, `HTTP_MIN/MAX_DELAY_SEC`, `HTTP_MAX_RETRIES`, `HTTP_PLAYWRIGHT_FALLBACK` |
| Sources | `SOURCE_ZAPIMOVEIS_ENABLED`, `SOURCE_QUINTOANDAR_ENABLED` |
| Condo (buy) | `CONDO_ENRICH_ENABLED`, `CONDO_SEARCH_ENABLED`, `CONDO_SEARCH_PROVIDER`, `CONDO_SEARCH_API_KEY`, `CONDO_SEARCH_MAX_PER_RUN`, `CONDO_SEARCH_MIN_CONFIDENCE` |

See `.env.example` for the full annotated list.

### Condo name + build year + age (buy, QuintoAndar only)

Apartment rows get `CondoName`, `BuiltYear`, `CondoAge` and `CondoSource` in Notion.

- **Name** comes free from the QuintoAndar detail page — it is already fetched during enrich.
- **Year** is served by QuintoAndar on only ~10% of listings. The rest is resolved from
  web-search snippets: one query per *building*, cached forever in `.cache/condos.json`,
  so the quota cost is a few hundred queries total. Leave `CONDO_SEARCH_API_KEY` blank to
  keep the search tier off — names still land, years stay sparse.
- Buildings are keyed by normalized street + number, so a name learned from one listing
  fills in every other unit in the same building, including partner classifieds that
  publish no condominium block of their own.
- A building is searched **once**. If no year comes back it is parked for good — the
  year simply is not in any snippet for it, and retrying costs a credit every run.
  `condo-stats` reports `parked_misses` vs `never_searched`; `condo-retry-misses`
  reopens the parked ones, which is only worth doing after changing the extractor.
- Condo data is display/analytics only — it never filters a listing out.
- `condo_overrides.csv` at the repo root (`key,name,built_year`) overrides everything;
  use it to hand-fix a building the search got wrong.

Details and the field-coverage measurements are in [PLAN-CONDO-AGE.md](PLAN-CONDO-AGE.md).

---

## Architecture

```
cli.py         ← typer entry (run-once / schedule / geo)
scheduler.py   ← APScheduler cron
pipeline.py    ← fetch → filter → upsert; then availability check
sources/
  base.py         ← Source ABC (search_rent, check_alive)
  zapimoveis.py   ← glue-api w/ zone-discovery cache
  quintoandar.py  ← yellow-pages JSON w/ bbox filters
condos.py      ← building key, name cleanup, registry (.cache/condos.json)
condo_search.py← year from SERP snippets (serper/brave/google_cse adapters)
http_client.py ← curl-cffi session, pacing, retry, cookie persist
notion_store.py← upsert / mark_gone / list_alive via data_sources.query
geo.py         ← Nominatim geocode + haversine distance, cached
config.py      ← pydantic-settings from .env
models.py      ← Listing dataclass
```

Anti-patterns guarded in code (do NOT change without testing):

- Zap uses `rentalTotalPriceMin/Max` (includes condo), NOT `priceMin/Max`.
- Zap requires `x-domain: www.zapimoveis.com.br` header — empty results without it.
- Zap `addressLocationId` format is `BR>Sao Paulo>NULL>Sao Paulo>{Zone}>{Bairro}` — zones auto-discovered + cached at `.cache/zap_zones.json`.
- QuintoAndar filters by bbox (pre-resolved via Nominatim to `.cache/bboxes.json`), not by neighborhood name.
- Notion `status` property type not creatable via API — we use `select` with `available`/`gone` options instead.
- notion-client 3.x: query via `data_sources.query(data_source_id=...)`, not `databases.query`.
