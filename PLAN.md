# Real-Estate Scraper — Implementation Plan

Target: Python scraper for rental listings on zapimoveis.com.br + quintoandar.com.br, pushed to Notion. Runs N×/day. Future: buy, sell, rent-for-others modules.

---

## Phase 0 — Documentation Discovery (CONSOLIDATED)

### Allowed APIs & patterns

**Zapimoveis (glue-api, unauthenticated)**
- Endpoint: `GET https://glue-api.zapimoveis.com.br/v2/listings`
- MANDATORY header: `x-domain: www.zapimoveis.com.br` (empty result without it)
- Filters: `business=RENTAL`, `listingType=USED`, `unitTypes=APARTMENT`, `rentalTotalPriceMin/Max` (includes condo+IPTU — use this not `priceMin/Max`), `bedrooms`, `bathrooms`, `suites`, `parkingSpaces`, `usableAreasMin`, `amenities=PETS_ALLOWED`, `from`, `size` (≤100), `page`.
- Neighborhoods: repeated `addressLocationId=BR>Sao+Paulo>NULL>Sao+Paulo>Barrios>NULL>Moema` tokens joined w/ comma. `levels=NEIGHBORHOOD`.
- Response path: `search.result.listings[].listing` → `id`, `address.geoLocation.location.{lat,lon}`, `pricingInfos[]` (pick `businessType=RENTAL`), `bedrooms[0]`, `bathrooms[0]`, `suites[0]`, `parkingSpaces[0]`, `usableAreas[0]`, `amenities[]`, `link.href`.

**QuintoAndar (yellow-pages JSON)**
- Endpoint: `POST https://www.quintoandar.com.br/api/yellow-pages/search`
- Filters by **bbox** (bounds_n/s/e/w), not name. Pre-resolve each neighborhood → bbox via `/api/yellow-pages/places-lookup` (or hard-code bboxes for our 9 bairros).
- Body: `filters.{map:{bounds_*}, totalCost:{minValue,maxValue}, area:{minValue}, bedrooms, bathrooms, suites, parkingSpaces, amenities:["pets_allowed"]}`, `business_context:"RENT"`.
- Listing URL: `https://www.quintoandar.com.br/imovel/<id>`.
- Fields: `id`, `address`, `location.lat/lon`, `rent`, `condominiumFee`, `totalCost`, `area`, `bedrooms`, `bathrooms`, `suites`, `parkingSpaces`, amenities.

**Notion API (notion-client 2.x)**
- Install: `pip install notion-client`
- Create DB: `notion.databases.create(parent={"type":"page_id","page_id":PARENT}, title=[...], properties={...})`
- Query: `notion.databases.query(database_id=..., filter={"property":"ExternalID","rich_text":{"equals":id}})`
- Create page: `notion.pages.create(parent={"database_id":...}, properties={...})`
- Update page: `notion.pages.update(page_id=..., properties={...})`
- Rate limit: 3 req/s avg, respect `Retry-After` on 429.
- **Setup gotcha**: user MUST share parent page with integration via "…" → Connections → add integration. Without this: `object_not_found`.
- Parent page ID parsed from user URL: `324ebb24e70f4796a95b2948c6fdd8cf` → formatted as `324ebb24-e70f-4796-a95b-2948c6fdd8cf`.

**Geocoding + distance**
- Work address → lat/lng: Nominatim (`geopy.geocoders.Nominatim`, user_agent required, 1 req/s max) or cached to disk after first run.
- Distance: `haversine` pkg (`pip install haversine`) — straight-line km. 5.5km threshold fine for SP central district.

**Anti-bot stack**
- HTTP: `curl-cffi` w/ `impersonate="chrome124"` (TLS fingerprint bypass for Cloudflare).
- Fallback: `playwright` + `playwright-stealth` if glue-api returns 403.
- Pacing: 3–10s random sleep between req, exponential backoff 1s→2s→4s→8s on 429/403.
- Headers: realistic UA + `Accept-Language: pt-BR,pt;q=0.9,en;q=0.8`, `Accept: application/json`, `Origin`, `Referer`.
- Session: persist cookies per source site; rotate on 403.
- Scheduling: `apscheduler` `BackgroundScheduler` w/ `CronTrigger` (2×/day configurable).

### Anti-patterns (DO NOT)
- Don't use `requests` directly against zapimoveis/quintoandar → 403 fast.
- Don't use `priceMin/Max` on zap for rent — misses condo fees. Use `rentalTotalPriceMin/Max`.
- Don't filter QuintoAndar by neighborhood name string — only bbox works.
- Don't invent Notion `status` property via API — not settable; use `select` w/ options `available`/`gone`.
- Don't hardcode integration token in code — env only.
- Don't skip `x-domain` header on zap.
- Don't parallelize source requests across neighborhoods without rate cap.

---

## Phase 1 — Project scaffold

**Package manager: uv** (astral-sh/uv). All commands use uv.

**Bootstrap:**
```
uv init --package --lib rental-finder   # or --app; we use --lib since cli is exposed via module
uv python pin 3.12
uv add curl-cffi notion-client apscheduler pydantic pydantic-settings haversine geopy tenacity python-dotenv structlog
uv add --optional playwright playwright playwright-stealth   # fallback only
uv add --dev pytest pytest-asyncio responses
```

**Files to create:**
```
.env.example         # all params, fully documented
.env                 # user-filled copy (gitignored)
.gitignore
.python-version      # 3.12 (created by uv python pin)
pyproject.toml       # managed by uv; deps pinned via uv.lock
uv.lock              # generated
src/rental_finder/
  __init__.py
  config.py          # pydantic-settings Settings class reads .env
  models.py          # Listing dataclass (source, external_id, url, price_rent, price_total, bedrooms, bathrooms, suites, parking, sqm, pets, lat, lng, neighborhood, first_seen, last_seen, status)
  geo.py             # geocode_work_addr(), distance_km(), NEIGHBORHOOD_BBOXES dict
  http_client.py     # CurlSession class w/ impersonate + retry + pacing
  sources/
    __init__.py
    base.py          # class Source(ABC): search_rent(filters) -> Iterator[Listing]; check_alive(listing) -> bool
    zapimoveis.py    # ZapSource
    quintoandar.py   # QuintoAndarSource
  notion_store.py    # NotionStore: ensure_database(kind="rent"), upsert_listing(listing), mark_gone(listing), list_current()
  pipeline.py        # run_rent_cycle(): for each source → fetch → filter by distance/params → upsert; then check_availability() on existing alive pages
  scheduler.py       # start_scheduler(cron_times)
  cli.py             # `python -m rental_finder run-once` and `python -m rental_finder schedule`
scripts/
  setup_notion.py    # one-time: create rent DB inside parent page, persist DB id to .env or a state file
tests/
  test_config.py
  test_geo.py
  test_zapimoveis_parser.py   # use saved JSON fixtures
  test_quintoandar_parser.py
  test_notion_store.py        # mock notion client
```

**Verification:**
- `uv sync` resolves cleanly.
- `uv run python -c "from rental_finder.config import settings; print(settings)"` loads .env w/o errors.

---

## Phase 2 — Config + geo

1. Implement `config.py` using `pydantic-settings`. Types: `NotionSettings`, `SearchFilters`, `SourceSettings`, `ScheduleSettings`.
2. Implement `geo.py`:
   - `WORK_ADDRESS` from .env → geocode once via Nominatim → cache lat/lng to `.cache/work_coords.json`.
   - `NEIGHBORHOOD_BBOXES`: hardcoded dict for the 9 SP bairros (lookup via Nominatim once at init, cache to `.cache/bboxes.json`).
   - `distance_km(a, b)` via `haversine`.

**Verification:**
- Unit test: `distance_km((-23.586,-46.672),(-23.590,-46.680)) < 1.5`.
- Run: `uv run python -m rental_finder.geo` prints work coords + all bboxes.

---

## Phase 3 — HTTP client + sources

1. `http_client.py`:
   - `CurlSession(impersonate="chrome124")` wrapping `curl_cffi.requests.Session`.
   - Pluggable rate limiter (token bucket, 1 req / 3-10s random).
   - `@tenacity.retry` on 429/403 w/ exponential backoff, max 5 tries.
   - Cookie jar persisted per source to `.cache/cookies_<source>.pkl`.

2. `sources/zapimoveis.py`:
   - Build URL per neighborhood (one request per bairro → simpler pagination).
   - Loop `page=1..N` until `listings` empty or size < page_size.
   - Parse `pricingInfos` finding `businessType=="RENTAL"` for total price.
   - Convert to `Listing`.

3. `sources/quintoandar.py`:
   - For each bairro bbox → POST search → paginate via `offset`.
   - Parse response `hits`.
   - Convert to `Listing`.

**Verification:**
- Save 1 real JSON response per source to `tests/fixtures/`. Parser tests read fixtures → assert N listings parsed w/ expected fields.
- Manual: `uv run python -m rental_finder.sources.zapimoveis --dry-run` prints first 3 listings.

---

## Phase 4 — Notion store

1. `scripts/setup_notion.py`:
   - Reads `NOTION_TOKEN` + `NOTION_PARENT_PAGE_ID`.
   - Creates DB "Rent Listings — São Paulo" with schema below.
   - Prints the new DB id; user pastes into `.env` as `NOTION_RENT_DB_ID`.

2. Schema (properties):
   - `Name` (title) — "{Neighborhood} · {Bedrooms}BR · R$ {total}"
   - `URL` (url)
   - `Source` (select: zap, quintoandar)
   - `ExternalID` (rich_text) — unique key
   - `Status` (select: available, gone)
   - `Neighborhood` (select: the 9 bairros)
   - `PriceRent` (number, BRL)
   - `PriceTotal` (number, BRL) — total incl condo
   - `Bedrooms` (number), `Bathrooms` (number), `Suites` (number), `Parking` (number), `Sqm` (number)
   - `Pets` (checkbox)
   - `DistanceKm` (number)
   - `Address` (rich_text)
   - `FirstSeen` (date), `LastSeen` (date)

3. `notion_store.py`:
   - `upsert_listing(listing)`: query by `ExternalID`; if exists → update `LastSeen` + `Status=available`; else → create page.
   - `mark_gone(external_id)`: set `Status=gone`.
   - `list_alive()`: query `Status=available`, yield listings.
   - Wrap all calls w/ tenacity respecting `Retry-After`.

**Verification:**
- `uv run python scripts/setup_notion.py` creates DB (requires user to share page with integration first — print clear error if 404).
- Unit test: mock `notion_client.Client`, assert correct payload shape.

---

## Phase 5 — Pipeline + availability check

1. `pipeline.run_rent_cycle()`:
   - For each enabled source: iterate neighborhoods in preference order → fetch → filter (price, bedrooms, baths, suites, sqm, parking, pets, distance ≤ 5.5km) → `upsert_listing`.
   - After ingestion: `for each alive in notion: if not source.check_alive(id): mark_gone()`.
   - `check_alive`: HEAD/GET the listing URL, 404 or "removed" banner → gone. Rate-limited.

2. Logging: structlog or plain logging to `logs/rent-YYYY-MM-DD.log`.

**Verification:**
- `uv run python -m rental_finder run-once` full cycle, logs listing count per source + upsert count.

---

## Phase 6 — Scheduler

1. `scheduler.py`:
   - `APScheduler BackgroundScheduler` w/ `CronTrigger` parsed from `SCHEDULE_CRON_TIMES` (default `"0 9,18 * * *"` = 2×/day 09:00 + 18:00 BRT).
   - Timezone from env (`SCHEDULE_TZ=America/Sao_Paulo`).
   - `uv run python -m rental_finder schedule` blocks, runs forever. Or `uv run rental-finder schedule` via [project.scripts] entry point.

**Verification:**
- Start scheduler, confirm first fire time logged. `Ctrl+C` clean shutdown.

---

## Phase 7 — Verification pass

1. Grep for anti-patterns:
   - `grep -r "priceMin" src/` → only in zap source as a comment flagging NOT to use.
   - `grep -r "import requests" src/` → empty (only curl-cffi).
   - `grep -rn "NOTION_TOKEN" src/` → only `config.py` (no hardcoded tokens).
2. Run full test suite.
3. Dry-run against both sources; eyeball 3 listings per source for field correctness.
4. End-to-end: run cycle, verify Notion DB populated w/ no duplicates after 2 runs.

---

## Future modules (stub interfaces now, implement later)
- `sources/base.py` already abstracts `search_rent()` → add `search_buy()`, `search_sell()`, `search_rent_for_others()`.
- `notion_store.ensure_database(kind="buy"|"sell"|"rent_for_others")` — reuse same parent page, new DB per kind.
- `.env` params prefixed per module (e.g. `RENT_PRICE_MIN`, `BUY_PRICE_MIN`).
