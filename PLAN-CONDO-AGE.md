# Buy module — Condo name + build year + age (QuintoAndar only)

Target: for every **apartment** in the buy pipeline, capture the condominium name, resolve the building's construction/delivery year, and derive its age in years. Surface all of it in the buy Notion DB so age becomes a first-class sort/filter dimension.

**Source scope: QuintoAndar only.** ZapImóveis is explicitly out of scope for this feature (see "Out of scope"). No zap code is touched.

Scope principles (same as PLAN-SALE.md):
- Reuse: `Listing`, `NotionStore.ensure_columns`, `QuintoAndarSource.enrich`, the ThreadPool fan-out in `_process_listing`.
- Isolate: all condo resolution behind one new module (`condos.py`) + one cache file + one search-API client.
- Buy-only by default; the same code path works for rent later behind a flag.

---

## Phase 0 — Field-availability findings (ALREADY PROBED, 2026-08-06)

Verified live against `/imovel/<id>/comprar` → `__NEXT_DATA__`. **n=29 buy listings across Moema, Campo Belo, Brooklin.** Do not re-derive.

`props.pageProps.initialState.house.houseInfo` carries:

| Field | Coverage | Note |
|---|---|---|
| `condominium.name` | **29/29** ✅ | e.g. `"Maison D Or"` |
| `condominium.hashId` | **29/29** ✅ | stable QA condo id, e.g. `ed5zsnvmyd` |
| `condominium.address` + `.number` | **29/29** ✅ | `"Avenida Ibijaú"` / `"191"` |
| `address.zipCode` | **29/29** ✅ | `"04524-020"` |
| `constructionYear` | **3/29 (~10%)** ❌ | `1988`, `1990`, `2010` |
| `generatedDescription` (year in text) | **0/29** ❌ | no fallback there |

Three consequences that shape the design:

1. **Name is solved for free.** The detail page is already fetched by `QuintoAndarSource._enrich_buy` ([src/rental_finder/sources/quintoandar.py:499](src/rental_finder/sources/quintoandar.py#L499)). Zero extra HTTP.
2. **Year is NOT solved by QuintoAndar.** ~10% is not a feature. An external year source is required.
3. **Cross-fill inside QA is worthless.** 29 listings → 27 distinct condos; only 2 condos had >1 unit, and in **0** cases did one sibling carry a year the other lacked. Do not build "learn the year from another unit in the same building".

Also verified and rejected:
- Condo page `/condominio/<slug>-<hashId>` renders **no** construction year (i18n keys `constructionYearLong` / `condoAge` exist in the bundle; no value is served).
- Region directory `/condominios/sao-paulo-sp-brasil/<region>` exposes ~90 condos of `totalCondominiums: 1350` (Moema); no pagination works; `sitemap.xml` → 403. No bulk condo index is scrapable.

**Name hygiene is required.** Real sampled values: `"ominium Club Ibirapuera"` (truncated), `"do Edifício Twins Towers - (Moema, SP)"`, `"ILHA DE CRETA - Campo Belo"`. Clean, but keep the raw.

---

### Phase 0 corrections found during implementation (2026-08-06)

The Phase 0 sample above was drawn from an unfiltered search URL and is **not**
representative of what the pipeline actually ingests. Measured against the
pipeline's own `_fetch_buy_type_houses("Moema", "apartamento")` (49 results):

1. **~2/3 of the buy result set are partner classifieds.** 15 own listings vs 34
   classifieds. They are marked by `listingTags: ["CLASSIFIED"]` /
   `recommendationListingOrigin: "CLASSIFIED"` in the search hit.
2. **Classifieds live at `/classificado/<id>/comprar`.** The code was building
   `/imovel/<id>/comprar` for every listing, which for a classified redirects to
   `/indisponivel/...` and returns an unhydrated 64-key shell. So `_enrich_buy`
   was silently returning nothing — no lat/lng, no condo fee, no suites — for two
   thirds of QA buy rows, and their stored Notion URL pointed at a dead page.
   Fixed here (`_is_classified` + `_buy_detail_url`), because condo enrichment is
   impossible without it.
3. **Classifieds serve no `zipCode` at all** (0/8 sampled), while `street` lands
   on 8/8. The building key is therefore **normalized street + number**, not
   CEP + number as originally planned. CEP is kept as registry metadata.
4. **Cross-fill between listings IS worth it** — the opposite of what the
   unfiltered sample suggested. Observed live: classified `117366229` inherited
   `Edifício Premium Point` from own-route listing `893357964`, same key
   `avenida doutor cardoso de melo|71`. Classifieds frequently omit the
   condominium block; the registry fills it from a sibling.
5. Classifieds that omit the block entirely also omit the number, so they have no
   key and stay unresolved (4/12 in the smoke run).

Measured on a mixed 12-listing smoke run, search tier off: keyed 8/12, named 7/12,
year 2/12.

---

## Phase 0b — Year via web-search API (PROBED, this is the primary source)

The condo name + street + number is enough to find the year on the open web. Tested live on 4 buildings that QuintoAndar left null:

| Condo | Address | Result | Evidence in snippet |
|---|---|---|---|
| Edifício Rousseau | Al. Jauaperi, 1120 | **1982** ✅ | "construído em 1982" |
| Les/Le Grand Palais | R. Gaivota, 1027 | **1995** ✅ | "construído em 1995 (há 29 anos)" |
| Park Lane | R. Min. Gabriel de Rezende Passos, 319 | **2023** ✅ | "entregue em julho de 2023" |
| Mustique | R. Arandu, 222 | **2005** ✅ | "construído em 2005", "pela EZTEC em 2005" |

**4/4.** The year is in the SERP title/snippet — no page fetch needed, which matters because the sites that hold this data are hard-blocked: `imovelweb.com.br` 403, `casamineira.com.br` 403, `loft.com.br` 403, `123i.com.br` 403 (only `npiconsultoria.com.br` returned 200). **Design rule: read snippets from the search API, never fetch the result pages.** Cheaper, faster, and stays clear of their bot walls.

Recurring domains that carry a ficha técnica: `loft.com.br`, `imovelweb.com.br`, `vivareal.com.br`, `zapimoveis.com.br/condominio/`, `123i.com.br`, `casamineira.com.br`, `lopes.com.br`, `npiconsultoria.com.br`, `imovelguide.com.br`, `agenteimovel.com.br`, `b4k.com.br`.

**Volume is trivial.** One query per *building*, cached forever — not per listing, not per run. 29 listings → 27 buildings. Across the 9 bairros under the buy filter, expect a few hundred buildings total, then a handful of new ones per week. Any provider's free tier covers the backfill.

**Provider choice** (pick one at implementation, all are drop-in behind the same interface):
- **Serper.dev** — Google SERP as JSON, 2,500 free credits then ~$0.30–1.00/1k. Recommended: Google-quality results at the lowest per-query cost, and the free tier alone likely covers the entire backfill.
- **Brave Search API** — 2,000 queries/month free, independent index.
- **Google Custom Search JSON API** — official, 100/day free, $5/1k after. Most expensive per query, shortest snippets.

Put it behind `condos.search_year(name, street, number, neighborhood)` so the provider is one env var and one adapter, never a leak into pipeline code.

**Query shape** (what was tested, keep it):
```
condomínio "<name>" "<street>, <number>" <neighborhood> São Paulo ano de construção
```
Quoting the name and the `street, number` pair is what pins the right building — `"Park Lane"` alone is a generic name that would match anywhere.

**Extraction rules** — regex over title + snippet, in priority order:
1. `constru[íi]d[oa] em (19|20)\d\d` / `entregue em (?:\w+ de )?(19|20)\d\d` / `ano de constru[çc][ãa]o:?\s*(19|20)\d\d` → explicit year.
2. `h[áa] (\d+) anos` → `current_year - N` (only if no explicit year found; mark lower confidence).
3. Plausibility gate: `1930 <= year <= current_year + 5`, else discard.

**Confidence + guards** (this is where the feature earns or loses trust):
- **Address must corroborate.** Discard any result whose title+snippet does not contain the street number and a distinctive street token. Without this, `"Park Lane"` in another city poisons the record.
- **Domain whitelist** for year extraction; **blacklist company registries** (`diariocidade`, `econodata`, `cnpj.*`) — the Rousseau search surfaced "empresa fundada em 21/01/1985", which is a CNPJ record, not the building. That single case is the whole argument for the blacklist.
- Agreement across **≥2 independent domains** → `confidence=high`. Single domain → `medium`. Conflicting years → take the modal value, record the conflict, mark `medium`.
- Always store `year_source_url` + `year_confidence` in the registry so any wrong value is traceable and fixable by hand.
- Rate-limit and back off like every other client here; reuse `CurlSession`'s pacing, or the provider SDK with the same delay settings.

**Optional tier**, only if regex coverage disappoints: send the top-5 snippets to an LLM for extraction. Do not start here — regex handled 4/4 phrasings, and it keeps the project dependency-free.

---

## Phase 1 — Model + config (no behavior change)

`src/rental_finder/models.py` — add to `Listing`:
```python
condo_name: str = ""            # cleaned, e.g. "Twins Towers"
condo_name_raw: str = ""        # as sourced, for debugging
condo_key: str = ""             # "04524020|191"  (CEP + number)
condo_hash: str = ""            # QA condominium.hashId
built_year: int | None = None
condo_age_years: int | None = None
condo_source: str = ""          # "quintoandar" | "search" | "manual" | "tpcl"
```
`condo_age_years` is derived, never stored upstream: `max(0, date.today().year - built_year)`, recomputed on every upsert so it can't go stale. A future-dated launch yields `0`, never negative.

`src/rental_finder/config.py` — add:
```python
condo_enrich_enabled: bool = True
condo_apartment_types: str = "Apartamento"      # matched against Listing.property_type
condo_cache_file: str = "condos.json"
condo_overrides_file: str = "condo_overrides.csv"

condo_search_enabled: bool = True
condo_search_provider: str = "serper"           # "serper" | "brave" | "google_cse"
condo_search_api_key: str = ""
condo_search_max_per_run: int = 50              # hard cap, protects the quota
condo_search_min_confidence: str = "medium"     # below this, store nothing
```
`.env.example` gets the new keys with empty values + a comment on where to get them.

Tests: `tests/test_condo_model.py` — age math, `built_year=None` stays null, future year clamps to 0.

---

## Phase 2 — `condos.py`: key, name cleanup, registry

New file `src/rental_finder/condos.py`.

**Building key — CEP + number.** QA gives both 29/29, and it is a far stronger join key than a normalized street name:
```python
def condo_key(zip_code: str, number: str | int) -> str   # "04524-020", "191" -> "04524020|191"
```
- CEP digits only, must be 8 or the key is empty (unresolvable → bail early, log `condo_key_missing`).
- number: first digit run (`"191-A"` → `"191"`).
- Gotcha to encode as a test: QA sometimes packs the number into the street (`street="Avenida Agami 333"`, `number=None`) — split a trailing number out before keying.

**Name cleanup.** `clean_condo_name(raw) -> str`:
- strip leading prepositions (`"do "`, `"da "`, `"de "`)
- strip trailing ` - <Bairro>` / ` - (<Bairro>, SP)`
- normalize the `Edifício `/`Ed. `/`Condomínio ` prefix to one canonical form
- title-case only when the raw is fully upper (`"ILHA DE CRETA"` → `"Ilha de Creta"`); never re-case mixed input
- keep the original in `condo_name_raw`

The cleaned name is also what feeds the search query, so this function's quality directly drives Phase 3's hit rate.

**Registry.** `CondoRegistry` backed by `.cache/condos.json`, thread-safe (`threading.Lock`, atomic write — same pattern as `NotionStore`):
```json
{
  "04524020|191": {
    "name": "Maison D Or",
    "name_raw": "Maison D Or",
    "built_year": 1988,
    "year_source": "quintoandar",
    "year_confidence": "high",
    "year_source_url": null,
    "street": "Avenida Ibijau", "number": "191",
    "qa_hash": "ed5zsnvmyd",
    "searched_at": null,
    "updated": "2026-08-06"
  }
}
```
The registry is what makes the search affordable: a building resolved once is never queried again. It also records `searched_at` for misses, so a building with no year found is not re-queried every single run — retry no sooner than, say, 90 days.

```python
registry.learn(key, *, name=None, built_year=None, source, **meta) -> None
registry.lookup(key) -> CondoInfo | None
```
`learn` never downgrades: precedence `manual > quintoandar > search > tpcl`. QuintoAndar outranks search because it is first-party structured data, not a snippet. A present value is never overwritten by `None`. One precedence constant, one place.

**Wiring** — `QuintoAndarSource._enrich_buy` reads `houseInfo.condominium.{name,hashId,number}` + `houseInfo.address.zipCode` + `houseInfo.constructionYear`, sets the `Listing` fields, calls `registry.learn(...)`. Same three lines in `_enrich_rent` behind the flag, for later.

**Pipeline hook** — in `_process_listing` ([src/rental_finder/pipeline.py:80](src/rental_finder/pipeline.py#L80)), after `src.enrich(lst)`, before the distance check:
```python
if settings.condo_enrich_enabled and _is_apartment(lst):
    resolve_condo(lst)   # registry + overrides only in this phase; no network
```
Condo data **never** filters a listing. A missing year must not drop anything — display/analytics only.

Tests: `tests/test_condo_key.py` (CEP formats, `"191-A"`, number-in-street, missing CEP), `tests/test_condo_name.py` (the four real junk samples), `tests/test_condo_registry.py` (precedence, no-downgrade, concurrent `learn`).

---

## Phase 3 — Search-year resolver

`src/rental_finder/condo_search.py` — provider adapter + extractor, per Phase 0b.

```python
def search_year(name, street, number, neighborhood) -> YearHit | None
# YearHit = (year: int, confidence: str, source_url: str, domain: str)
```

Called from `resolve_condo()` **only when** all of: `condo_search_enabled`, the registry has no year, `searched_at` is null or older than 90 days, and the per-run counter is under `condo_search_max_per_run`. Result goes straight into `registry.learn(..., source="search")`.

Threading: the pipeline already runs `_process_listing` on a `ThreadPoolExecutor` ([src/rental_finder/pipeline.py:127](src/rental_finder/pipeline.py#L127)). The search-call counter and the provider's rate limit both need a lock — put the whole `search_year` call behind a semaphore sized to the provider's QPS (Brave is 1/s; Serper tolerates more).

Log every attempt: `condo_search` with name, key, hit/miss, year, confidence, domain. This is the only way to tell a bad query template from a genuinely undocumented building.

Tests (no network — fixture SERP JSON captured from a real response):
- the four real phrasings above → correct year
- `"há 29 anos"` relative → `current_year - 29`, confidence `medium`
- CNPJ-registry snippet ("empresa fundada em 21/01/1985") → **rejected** by blacklist
- right name + wrong address in snippet → **rejected** by the corroboration gate
- year `1899` / `2099` → rejected by the plausibility gate
- two domains agreeing → `high`; one → `medium`; conflict → modal + `medium`

---

## Phase 4 — Notion surface

`NotionStore._COLUMN_SPEC` ([src/rental_finder/notion_store.py:76](src/rental_finder/notion_store.py#L76)) — add:
```python
"CondoName":   {"rich_text": {}},
"CondoKey":    {"rich_text": {}},
"BuiltYear":   {"number": {"format": "number"}},
"CondoAge":    {"number": {"format": "number"}},
"CondoSource": {"select": {}},
```
`_COLUMN_SPEC` is shared by rent and buy, so both DBs gain the columns; they stay empty on rent until the flag flips. If buy-only columns are wanted, split into `_COLUMN_SPEC_COMMON` + `_COLUMN_SPEC_BUY` and merge in `ensure_columns(kind)` — ~10 lines either way.

`_properties()` buy branch emits all five, null-safe (`{"number": None}`, `{"select": None}`). The existing `_strip_missing` / `_extract_missing_prop` path already tolerates a DB that predates the columns, so no migration is needed for old rows.

`CondoKey` is written deliberately: the stored `Address` is a joined string that **drops the street number** ([src/rental_finder/notion_store.py:149](src/rental_finder/notion_store.py#L149)), so without an explicit key column the Phase 5 backfill would be blind on existing rows.

Suggested Notion views (manual, document in README): sort `CondoAge` asc; filter `CondoAge < 10`.

---

## Phase 5 — CLI + backfill

`src/rental_finder/cli.py`:
- `condo-backfill --kind buy [--max N]` — walk `store.list_alive(kind)`, resolve by `CondoKey` from registry, run searches for the misses under the `--max` cap, patch the five properties. Same shape as `recheck_gone` ([src/rental_finder/pipeline.py:285](src/rental_finder/pipeline.py#L285)): no search fan-out, iterate + update, ThreadPool at `POOL_WORKERS`. Rows created before Phase 4 have no `CondoKey` and are skipped — they self-heal on the next buy cycle.
- `condo-stats` — resolved/total per bairro, per source, per confidence; plus how much search quota was spent.
- `condo-search <name> <street> <number>` — one-shot debug of the query + extraction, prints the raw snippets. You will want this the first time a building resolves wrong.

---

## Phase 6 — Verification

1. `uv run pytest` — new unit tests green.
2. One live buy cycle on Moema with `condo_enrich_enabled=true`, `condo_search_max_per_run=10`. Assert in the log: `condo_resolved`, `condo_unresolved`, `condo_search` hit rate, and that **no** listing was dropped over condo data.
3. Hand-check every `built_year` resolved by search in that first run against its `year_source_url`. Small N, do it once, properly — this is the run that tells you whether the corroboration gate is tight enough.
4. Sanity gate: `CondoName` ~100% of apartment rows; `BuiltYear` ~10% before Phase 3 and materially higher after. If the search tier lands under ~60%, the query template is wrong before the data is.

**Operational note:** `MODULES_ENABLED=rent` in `.env`, and the scheduler only registers the buy job when `"buy" in modules_list` ([src/rental_finder/scheduler.py:39](src/rental_finder/scheduler.py#L39)). Today buy runs only via `run-buy-once`. Flip to `rent,buy` when this ships, or the feature never runs on a schedule.

---

## Expected coverage

| | Condo name | Build year |
|---|---|---|
| After Phase 2 | ~100% (29/29 sampled) | ~10% (3/29 sampled) |
| After Phase 3 | ~100% | 4/4 on the probe — treat as promising, not proven; measure in Phase 6 |

Phase 2 + 4 is shippable alone (names land, years sparse). Phase 3 is what makes "idade em anos" a real column.

---

## Deferred alternative — TPCL / GeoSampa

The SP municipal IPTU fiscal cadastre (ex-TPCL, open data since 2016, ~3M records) carries an authoritative construction year per property, address-keyed — a natural fit for the CEP+number key. It is strictly more reliable than SERP snippets and has no quota.

It is **deferred, not rejected**: it costs a 21 GB source, a fixed-width/CSV parse, a lot-vs-unit collapsing rule for vertical condos, and a yearly refresh, versus a probe that already worked 4/4 for a handful of API calls. Revisit only if the search tier's measured coverage or accuracy disappoints in Phase 6. If revisited: build `.cache/condo_years.sqlite` keyed by the same `condo_key()`, slot it in as another tier in `resolve_year`, precedence just under `manual`.

Sources: [Base dos Dados — Cadastro do IPTU SP](https://basedosdados.org/dataset/05f1b96d-883b-4202-a4bd-40379c5d326a), [GeoSampa — IPTU metadata](https://metadados.geosampa.prefeitura.sp.gov.br/geonetwork/srv/api/records/b971efa1-aad5-42f3-ad28-18f1f4efe270)

---

## Out of scope (explicit)

- **ZapImóveis, entirely.** For the record: zap's glue API returns `condominiumName = ""` on 30/30 sampled USED sale listings, and its detail page is 403 Cloudflare via both `curl_cffi` and a headless browser. (Unrelated but noted while probing: `ZapSource.check_alive` ([src/rental_finder/sources/zapimoveis.py:422](src/rental_finder/sources/zapimoveis.py#L422)) always returns `True` because of that same 403 — separate bug, separate task.)
- **Fetching the condo pages themselves** (loft/imovelweb/123i/casamineira). All 403; snippets are enough. Do not add a scraper for them.
- Rent module surfacing (code path is flag-ready; do not enable).
- ~~Filtering or scoring by age — display only for now.~~ **Shipped 2026-09-19:** a year at
  `medium`+ confidence over `CONDO_MAX_AGE_YEARS` (40) now drops the listing at discovery
  and trashes any row it already had; `condo-purge-old` sweeps the stored base. An unknown
  or uncorroborated year still keeps the listing.
- `listingType=DEVELOPMENT` launches and their delivery calendars.
- Any Notion schema migration beyond the additive columns.

## Execution order

1 (model/config) → 2 (`condos.py` + QA wiring) → 4 (Notion) → 3 (search resolver) → 5 (CLI/backfill) → 6 (verify).
