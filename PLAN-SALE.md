# Extend rental-finder — Sale (Buy) module

Target: add "for sale" ingestion mirroring the existing rent pipeline. Same neighborhoods, same work-distance cap, same sources (zapimoveis + quintoandar), new Notion DB, new env-var group, shared code where safe.

Scope principles:
- Reuse: `Listing` dataclass, `CurlSession`, `geo.*`, `NotionStore` infra, scheduler.
- Isolate: source search methods, filter config, Notion DB id, CLI entry.
- Naming: keep existing `rent_*` settings intact; add parallel `sale_*` set (do NOT rename rent_*).

---

## Phase 0 — Documentation Discovery (ALWAYS FIRST)

Deploy one subagent per external API to confirm 2026-current sale endpoints. Each must return:
1. Sources consulted
2. Exact request shape (params/headers/body) for SALE
3. Exact response path to per-listing `price`, `condoFee`, `iptu`, and geo
4. Anti-patterns (what 404s / returns empty)

### Subagent A — Zap sale API
Questions:
- Is `GET https://glue-api.zapimoveis.com.br/v2/listings` still the endpoint for SALE?
- Correct business param value: `SALE` (not `RENTAL`).
- Price filter param: `priceMin/priceMax` for SALE (NOT `rentalTotalPriceMin/Max`).
- Confirm `listingType=USED` still valid for resale; document `listingType=DEVELOPMENT` alternative for new builds.
- Response path: `search.result.listings[].listing.pricingInfos[].businessType == "SALE"` → which field holds sale price (`price` vs `monthlyRentalTotalPrice`)? Likely plain `price`.
- Monthly condo fee (`monthlyCondoFee`) and IPTU — surface for display only (not filter).
- Sort key for SALE: `pricing.price.price ASC` — verify.

Reference in code: [src/rental_finder/sources/zapimoveis.py:62](src/rental_finder/sources/zapimoveis.py#L62). Existing RENTAL path proves host + header + addressLocationId format; SALE differences are the only unknown.

### Subagent B — QuintoAndar sale API
Questions:
- QuintoAndar historically rent-first. Do they expose SALE listings via `/comprar/imovel/<slug>`? Check current site.
- SSR search URL pattern for sale: likely `/comprar/imovel/<nb-slug>-<city>-<state>-brasil/...`.
- `business_context` / body filter analog to `"RENT"` for SALE (candidate: `"SALE"` or `"BUY"`).
- `__NEXT_DATA__` extraction path for sale results — same `pageProps.initialState.houses`?
- If QuintoAndar SALE is unsupported or too sparse: document that and plan to disable `SOURCE_QUINTOANDAR_ENABLED` for sale mode (config flag per module).

Reference in code: [src/rental_finder/sources/quintoandar.py:34](src/rental_finder/sources/quintoandar.py#L34).

### Subagent C — Notion schema for sale DB
Questions:
- Are sale-specific number fields needed: `Price`, `CondoFee`, `Iptu`, `PricePerSqm`?
- Drop `PriceRent`/`PriceTotal` for sale DB; replace with `Price` + `CondoFee` + `Iptu`.
- Select options reuse `Source`, `Neighborhood`, `Status`, `PropertyType` as-is.
- setup_notion already has `kind=buy` title hook ([scripts/setup_notion.py:35](scripts/setup_notion.py#L35)) — confirm schema helper needs branching per kind.

### Consolidated output of Phase 0
Write findings to `docs/sale-api-notes.md` (new file). Fields required before Phase 1 starts:
- `ZAP_SALE_PARAMS`: finalized param list template
- `ZAP_SALE_PRICE_PATH`: exact pricingInfos pick rule
- `QA_SALE_URL_PATTERN`: confirmed slug format (or "unsupported → disable")
- `SALE_NOTION_SCHEMA`: property list diff vs rent schema

Anti-patterns to record:
- Do NOT reuse `rentalTotalPriceMin/Max` for SALE.
- Do NOT assume QuintoAndar sale supported without SSR verification.
- Do NOT share a Notion DB between rent and sale (separate DBs per kind).

---

## Phase 1 — Models + config (generalize, no behavior change)

**Goal**: make `Listing` and config carry mode-specific fields without breaking rent.

1. `src/rental_finder/models.py`
   - Add optional fields to `Listing`:
     - `mode: str = "rent"` — `"rent" | "sale"`.
     - `price_sale: float = 0.0` — sale price (zero for rent listings).
     - `condo_fee: float = 0.0` — monthly condo (shared for rent+sale display).
     - `iptu: float = 0.0` — annual or monthly IPTU as source provides; document unit.
   - Keep `price_rent` / `price_total` untouched (rent mode uses them).
   - Update `global_id` to include mode: `f"{self.mode}:{self.source}:{self.external_id}"`.

2. `src/rental_finder/config.py`
   - Add `sale_*` settings mirroring rent:
     - `sale_price_min: int`, `sale_price_max: int`
     - `sale_bedrooms_min/max`, `sale_bathrooms_min`, `sale_suites_min`, `sale_sqm_min`, `sale_parking_min`
     - `sale_property_types: str = ""`
     - `sale_pets_required: bool = False` (typically not a sale filter — default off)
     - `sale_furnished_allowed: bool = True`
     - `sale_neighborhoods: str = ""` — fallback to `rent_neighborhoods` if empty via a property.
   - Add `notion_sale_db_id: str = ""`.
   - Add `source_zapimoveis_sale_enabled: bool = True`.
   - Add `source_quintoandar_sale_enabled: bool = False` (default off pending Phase 0 subagent B result).
   - Property helper: `sale_neighborhoods_list` → list, falling back to rent list.

3. `.env.example`: append a `# ----- Search: SALE module -----` block with every `SALE_*` key, commented defaults. Do not remove any existing keys.

Copy pattern from: [src/rental_finder/config.py:29-45](src/rental_finder/config.py#L29-L45) for the rent block; duplicate with `sale_` prefix.

**Verification**:
- `uv run python -c "from rental_finder.config import settings; print(settings.sale_price_min)"` loads without error when sale vars set.
- `uv run pytest tests/test_config.py` green.

**Anti-pattern guards**:
- Do NOT rename any `rent_*` field.
- Do NOT make sale config required — every var needs a default or empty fallback so existing `.env` still loads.

---

## Phase 2 — Source abstraction + Zap sale

**Goal**: Add `search_sale()` to `Source` base and implement in `ZapSource`.

1. `src/rental_finder/sources/base.py`
   - Add abstract method `search_sale(self, neighborhoods: list[str]) -> Iterator[Listing]`.
   - Default-raise `NotImplementedError` in base if a subclass doesn't override, so QuintoAndar can opt out without breakage (make it abstract but provide a no-op fallback in `QuintoAndarSource` if Phase 0-B says unsupported).

2. `src/rental_finder/sources/zapimoveis.py`
   - Copy the `_params()` method pattern ([src/rental_finder/sources/zapimoveis.py:125](src/rental_finder/sources/zapimoveis.py#L125)) as `_sale_params()`:
     - `business=SALE`
     - Replace `rentalTotalPriceMin/Max` → `priceMin/priceMax` (use values from Phase 0-A).
     - Remove rent-only `amenities=PETS_ALLOWED` unless `sale_pets_required` truthy.
     - Sort: update to `pricing.price.price ASC` (verify in 0-A).
   - Copy `_parse_one()` as `_parse_sale_one()`:
     - Pick pricing where `businessType=="SALE"`.
     - Map to `Listing(mode="sale", price_sale=price, price_rent=0, price_total=price, condo_fee=condo, iptu=iptu)`.
     - Re-use existing address, geo, bedrooms/bathrooms/suites/parking/sqm extraction (same response shape).
   - Implement `search_sale()` mirroring `search_rent()` ([src/rental_finder/sources/zapimoveis.py:224](src/rental_finder/sources/zapimoveis.py#L224)) with the new params/parser.

3. Zone resolver (`_resolve_zone`): reuse as-is; zones are geographic, independent of business type. Verify it still hits when probed with `business=SALE` — if empty, probe still works because it uses `business=RENTAL` for the probe (ok, zones don't depend on business). Leave untouched.

**Verification**:
- Save a fixture JSON response (one real hit) to `tests/fixtures/zap_sale_sample.json`.
- Unit test parses fixture → asserts `mode=="sale"`, `price_sale>0`, `price_total==price_sale+condo` (or per 0-A rule).
- Manual dry-run: `uv run python -m rental_finder.sources.zapimoveis --dry-run --mode sale` prints 3 listings (extend the `__main__` block).

**Anti-pattern guards**:
- `grep -n "rentalTotalPrice" src/rental_finder/sources/zapimoveis.py` — must not appear inside `_sale_params`.
- No `amenities=PETS_ALLOWED` hard-coded for sale.

---

## Phase 3 — QuintoAndar sale (conditional)

**Only execute if Phase 0-B confirms support.** Otherwise skip this phase and leave `source_quintoandar_sale_enabled=False`.

1. `src/rental_finder/sources/quintoandar.py`
   - Add `_sale_search_url(neighborhood)` mirroring `_search_url()` at [src/rental_finder/sources/quintoandar.py:46](src/rental_finder/sources/quintoandar.py#L46) with path prefix `/comprar/imovel/...` per Phase 0-B finding.
   - Add `_fetch_sale_houses()` reusing `_extract_next_data()`.
   - Add `_parse_sale_hit()` that maps to `Listing(mode="sale", price_sale=raw["salePrice"], ...)` using field names from 0-B.
   - Implement `search_sale()`.
   - If unsupported by 0-B: implement `search_sale()` as `yield from ()` (returns nothing) and log once.

**Verification**: same fixture-based test pattern as Phase 2. Skip test if source unsupported.

---

## Phase 4 — Pipeline + Notion store

**Goal**: add sale cycle with its own DB routing.

1. `src/rental_finder/notion_store.py`
   - Extend `_db_id()` ([src/rental_finder/notion_store.py:39](src/rental_finder/notion_store.py#L39)) to handle `kind="sale"` → `settings.notion_sale_db_id`.
   - Extend `_properties()` ([src/rental_finder/notion_store.py:80](src/rental_finder/notion_store.py#L80)) to branch on `lst.mode`:
     - For `"sale"`: emit `Price`, `CondoFee`, `Iptu` instead of `PriceRent`/`PriceTotal`.
     - Keep shared fields identical (Name, URL, Source, ExternalID, Status, Neighborhood, Bedrooms, Bathrooms, Suites, Parking, Sqm, PropertyType, DistanceKm, Address, FirstSeen, LastSeen).
     - Compose Name as `f"{neighborhood} · {bedrooms}BR · R$ {int(price_sale):,}"` (thousands sep).
   - Keep `_missing_props` self-healing behavior; it will absorb any schema drift.
   - `list_alive(kind="sale")` already parametrized — no change.

2. `src/rental_finder/pipeline.py`
   - Extract the existing `run_rent_cycle()` body ([src/rental_finder/pipeline.py:43](src/rental_finder/pipeline.py#L43)) into a generic `_run_cycle(mode: str, filters, search_fn_name: str, store_kind: str)`.
     - Replace hardcoded filter references with mode-aware accessors (new `_matches_filters(lst, mode)`).
     - Replace `src.search_rent` with `getattr(src, f"search_{mode}")`.
     - Use `store_kind=mode` (rent → rent DB, sale → sale DB).
   - Keep `run_rent_cycle()` as thin wrapper for back-compat: `return _run_cycle("rent", ...)`.
   - Add `run_sale_cycle()` → `_run_cycle("sale", ...)`.
   - `_matches_filters` branches on `lst.mode`:
     - `"rent"`: existing behavior against `rent_*` settings.
     - `"sale"`: same checks against `sale_*` settings; price check against `lst.price_sale` (not `price_total`).
   - Distance cap shared (reuse `work_max_distance_km`).

3. `enabled_sources()` ([src/rental_finder/sources/__init__.py:8](src/rental_finder/sources/__init__.py#L8))
   - Extend to accept `mode: str = "rent"`:
     - For `"rent"` use existing `source_zapimoveis_enabled` / `source_quintoandar_enabled` flags.
     - For `"sale"` use `source_zapimoveis_sale_enabled` / `source_quintoandar_sale_enabled`.

**Verification**:
- Extend `tests/test_filters.py` with `test_sale_match_ok`, `test_sale_price_out` using `mode="sale"` listings.
- Unit test `NotionStore._properties` with a sale listing — assert `Price` present, `PriceRent` absent.
- Unit test `run_sale_cycle` with mocked sources + mocked store (stub `data_sources.query`).

**Anti-pattern guards**:
- `grep -n "settings.rent_" src/rental_finder/pipeline.py` — no rent_* reads inside sale code path.
- Don't share Notion DB id between modes — `_db_id("sale")` must raise if `notion_sale_db_id` unset.

---

## Phase 5 — Notion DB creation + CLI + scheduler

1. Notion sale DB
   - `uv run python scripts/setup_notion.py --kind buy` — existing script already handles this path ([scripts/setup_notion.py:88](scripts/setup_notion.py#L88)).
   - BUT: the `_schema()` function ([scripts/setup_notion.py:42](scripts/setup_notion.py#L42)) is rent-specific (emits `PriceRent`/`PriceTotal`). Fork into `_schema(kind)`:
     - `"rent"`: current schema (unchanged).
     - `"buy"` / `"sell"`: replace price fields with `Price`, `CondoFee`, `Iptu`.
   - Output prints the DB id; user pastes into `.env` as `NOTION_SALE_DB_ID` (rename `NOTION_BUY_DB_ID` in output for clarity — **settle on `SALE` vs `BUY` naming in Phase 0 consolidation; this doc uses `sale_` to match "for sale" user phrasing**).

2. `src/rental_finder/cli.py`
   - Add command `run-sale-once`:
     ```python
     @app.command("run-sale-once")
     def run_sale_once() -> None:
         _init_logging()
         from .pipeline import run_sale_cycle
         typer.echo(run_sale_cycle())
     ```
   - Rename existing `run-once` entry in help text to `run-rent-once` alias (keep `run-once` as alias → `run_rent_cycle` for back-compat, or deprecate; decide based on whether users rely on it — check `scripts/run_daily.cmd` which calls `rental-finder run-once`).
   - Safer path: keep `run-once` pointing at rent (do not break the cron .cmd); add `run-sale-once` new.

3. `scripts/run_daily.cmd`
   - Leave untouched (runs rent cycle as before).
   - Consider: add a second .cmd `scripts/run_sale_daily.cmd` calling `uv run rental-finder run-sale-once`. Only create if user wants scheduled sale runs — ask before adding.

4. Scheduler (`src/rental_finder/scheduler.py`)
   - If scheduler currently only calls rent cycle: add a second job registration driven by `settings.modules_list` (already supports `"rent,sale"`).
   - Read current file first; adapt accordingly. Keep rent job wiring identical.

**Verification**:
- `uv run rental-finder --help` shows both `run-once` and `run-sale-once`.
- `uv run rental-finder run-sale-once` end-to-end populates the sale Notion DB (requires Phase 0 DB created + env var set).
- Existing `uv run rental-finder run-once` still works unchanged (regression check).

**Anti-pattern guards**:
- Do not remove or rename `run-once` command (cron depends on it).
- Do not share `notion_rent_db_id` for sale writes.

---

## Phase 6 — Verification pass

1. Regression: run existing rent suite end-to-end. Confirm zero diff in rent DB behavior (idempotent re-run → no duplicates, no unexpected `mode` field in rent entries if Notion schema not updated for rent — leave rent DB untouched).
2. Sale smoke test:
   - Set `.env` sale vars to a generous price band (e.g. 500000–900000 BRL).
   - Set `NOTION_SALE_DB_ID` from Phase 5 step 1.
   - `uv run rental-finder run-sale-once`.
   - Eyeball 3 entries in the sale Notion DB for correctness (price, URL, distance, neighborhood).
3. Grep audits:
   - `grep -rn "rentalTotalPrice" src/` — must NOT appear in sale code paths.
   - `grep -rn "business.*SALE" src/` — only in `zapimoveis._sale_params`.
   - `grep -rn "settings.rent_" src/rental_finder/` — only in rent-specific branches.
4. Test suite: `uv run pytest -q` all green.
5. Config compatibility: load settings with a pre-Phase-1 `.env` (no `SALE_*` keys) → no ValidationError.

---

## Out of scope (explicit)

- Multi-DB view / merging rent+sale in one table.
- Price-per-sqm ranking or filtering UI.
- "Rent for others" and "Sell" modules (they follow the same pattern; add after sale ships).
- Alert/notification channel for new sale matches (separate track).
- Migrations of existing rent DB schema.

---

## Execution order summary

```
Phase 0  → subagents A/B/C, write docs/sale-api-notes.md
Phase 1  → models + config + .env.example (no behavior change)
Phase 2  → Zap sale source + fixture test
Phase 3  → QuintoAndar sale source (skip if unsupported)
Phase 4  → pipeline + NotionStore branching + filter tests
Phase 5  → setup_notion schema fork + CLI command + scheduler hook
Phase 6  → regression + smoke + grep audit
```

Each phase is self-contained with its own verification. Safe to pause between phases and resume in a new chat; reference this file + the Phase 0 notes file.
