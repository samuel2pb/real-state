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
uv run rental-finder condo-backfill --kind buy --max-searches 20  # fill condo cols on existing rows
uv run rental-finder condo-lookup "Rua Jose Antonio Coelho" 603 --neighborhood "Vila Mariana"
uv run rental-finder condo-golden                      # re-resolve the hand-verified buildings
uv run rental-finder condo-invalidate --source search  # forget the SERP tier's years
uv run rental-finder condo-retry-misses                # reopen parked buildings
uv run rental-finder condo-purge-old --kind buy        # dry run: rows over the age cap
uv run rental-finder condo-purge-old --kind buy --apply  # trash them (Notion trash, 30 days)
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
| Condo (buy) | `CONDO_ENRICH_ENABLED`, `CONDO_SEARCH_ENABLED`, `CONDO_SEARCH_PROVIDER`, `CONDO_SEARCH_API_KEY`, `CONDO_SEARCH_MAX_PER_RUN`, `CONDO_SEARCH_MIN_CONFIDENCE`, `CONDO_AGENT_MODEL`, `CONDO_AGENT_MAX_USD`, `CONDO_AGENT_CONCURRENCY` |
| Age gate (buy) | `CONDO_MAX_AGE_YEARS`, `CONDO_AGE_FILTER_ENABLED`, `CONDO_AGE_MIN_CONFIDENCE`, `CONDO_AGE_REQUIRE_KNOWN` |

See `.env.example` for the full annotated list.

### Condo name + build year + age (buy, QuintoAndar only)

Apartment rows get `CondoName`, `BuiltYear`, `CondoAge`, `CondoSource` and
`CondoConfidence` in Notion.

- **Name** comes free from the QuintoAndar detail page — it is already fetched during enrich.
- **Year** is served by QuintoAndar on only ~10% of listings. The rest is resolved by
  `CONDO_SEARCH_PROVIDER=claude`: one `claude -p` run per *building*, with WebSearch and
  WebFetch, reading the ficha tecnica itself (Loft is the only site that publishes
  "Construcao" consistently for older zona-sul buildings). Cached forever in
  `.cache/condos.json`, so the cost is one lookup per building, ever.
- **Auth.** The agent tier uses the Claude Code login on this machine and needs no API key,
  but an interactive OAuth session expires and a scheduled run would then resolve nothing.
  For unattended runs create a long-lived token with `claude setup-token` and export
  `CLAUDE_CODE_OAUTH_TOKEN` (or set `ANTHROPIC_API_KEY`) in the environment the scheduled
  task runs under.
- **Nothing the agent says is taken on trust.** The year must be plausible, and the street
  number the agent reports having read must equal the number in the `CondoKey` — a real
  "Edificio Chibaras" built in 1973 exists at Avenida Chibaras 415, and that is not the
  building at 381. A mismatch, or an answer that names no address at all, is stored as a
  miss, never as a year.
- **`CondoConfidence` is written on every enriched row** — `high`, `medium`, `estimated` or
  `not_found`. Only `medium` and above write `BuiltYear` (`CONDO_SEARCH_MIN_CONFIDENCE`).
  Any ranking formula in Notion must require `high`/`medium` before it trusts `CondoAge`:
  the SERP tier's unqualified `medium` years are what put a 1977 building at the top of the
  base as a 2023 build.
- The old SERP-snippet tier is still selectable (`serper` / `brave` / `google_cse`) and still
  needs `CONDO_SEARCH_API_KEY`, but it reads years out of search snippets and cannot tell a
  ficha tecnica from a neighbourhood index page. `condo-invalidate --source search` drops
  everything it wrote so the agent resolves those buildings again.
- Buildings are keyed by normalized street + number, so a name learned from one listing
  fills in every other unit in the same building, including partner classifieds that
  publish no condominium block of their own. The agent does not need a name at all: it
  identifies an unnamed building from the address plus the unit's fee, size and room counts.
- A building is looked up **once**. If no year comes back it is parked — no public source
  states one. `condo-stats` reports `parked_misses` vs `never_searched`; `condo-retry-misses`
  reopens the parked ones, worth doing after changing the prompt.
- `condo-golden` re-resolves the 17 hand-verified buildings in
  `tests/fixtures/condo_golden.csv` and diffs the years, including four negative cases that
  must come back null. It costs one agent run per row.
- Condo data is display/analytics only, with one exception: the **age gate** below.

### Age gate (buy)

Over half of what the sources return is a building older than 40 years — 389 of the 905 rows
in the buy base when the gate shipped. Those rows never earn a place:

- `CONDO_MAX_AGE_YEARS` (default 40, `0` disables) caps the age. A listing past it is never
  upserted, and a row it already had is moved to Notion's trash on the spot.
- **No trusted year, no row** (`CONDO_AGE_REQUIRE_KNOWN`, default on). A year counts only at
  `CONDO_AGE_MIN_CONFIDENCE` (default `medium`) or better; unknown, `estimated` and `not_found`
  are rejected as `age_unknown`. That covers classifieds with no building address to look
  up, buildings no public source dates, blocked buildings, and `condo_house` listings (never
  resolved). A building merely not resolved *yet* — never searched, per-run cap
  (`CONDO_SEARCH_MAX_PER_RUN`) hit, agent unreachable — is rejected this run and tried again
  the next time the listing is fetched. Turn the flag off to keep unknown years instead.
- It costs nothing after the first cycle. The buy prefilter asks the registry for the year
  by street + number straight off the search hit, so a known-old building is dropped before
  its detail page is ever fetched — the same tier that already saves ~1000 fetches a run.
- `condo-purge-old` sweeps the rows already stored. Dry run by default; `--apply` trashes,
  `--no-include-gone` leaves gone rows alone, and `--resolve` additionally runs the agent on
  rows with no year at all (that one costs money — dry-run first to see how many).
- Rows are trashed, not deleted: Notion keeps them recoverable for 30 days.
- Zap rows are unaffected — zap publishes no condominium block, so nothing there has a year.
- `condo_overrides.csv` at the repo root (`key,name,built_year,block,why`) overrides
  everything. Set `built_year` to hand-fix a building the resolver got wrong, or
  `block=1` to say **no year may be stored for this building at all** — the resolver is
  never called, any cached year is dropped, and `CondoConfidence` reads `not_found`.
  `block` exists because the alternative way to overrule the resolver is to invent a
  year, which is the bug this whole tier is here to stop. Two buildings ship blocked:
  Rua Jose Antonio Coelho 750 and Rua Humberto I 928, where a hand check could not
  confirm the building and the agent answers anyway from a single source.

### What `Sqm` measures

Zap publishes `usableAreas` and `totalAreas` separately, so its rows carry `Sqm` (private
area), `SqmTotal` and `SqmBasis = usable`. QuintoAndar publishes exactly one unlabelled
`area` field — verified live on listing 125652767, where `area` was 141 for a ~119 m2
apartment, the difference being its two parking spaces — so its rows say
`SqmBasis = unspecified`. An R$/m2 computed on an `unspecified` row is a floor, not a
value. There is no way to split the two from QuintoAndar's payload.

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
condo_agent.py ← year + name via `claude -p` w/ WebSearch/WebFetch (prompts/)
condo_search.py← legacy: year from SERP snippets (serper/brave/google_cse)
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
