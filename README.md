# rental-finder

Searches **QuintoAndar** for apartments to **rent** or to **buy** in São Paulo, keeps only the
ones that match your criteria (price, bedrooms, size, distance from work, building age), and
writes them to a Notion database. It runs on your machine a few times a day and keeps each row
up to date: price drops, price rises, and listings that disappear.

---

## 1. Quick start

You need `git`. The installer adds [uv](https://docs.astral.sh/uv/) (the Python toolchain) if it
is missing, clones the repo, installs dependencies and starts the setup wizard.

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.ps1 | iex
```

It installs into `~/rental-finder` (`%USERPROFILE%\rental-finder` on Windows); set
`RENTAL_FINDER_HOME` to change that. Use `--ref <branch>` / `--no-init` with `install.sh`
(`curl ... | bash -s -- --ref dev`). On Windows set `$env:RENTAL_FINDER_REF` or
`$env:RENTAL_FINDER_NO_INIT = "1"` before the one-liner.

Then, from the install directory:

```bash
uv run rental-finder init                # wizard: writes .env, creates the Notion databases
uv run rental-finder doctor              # checks the setup, one line per item
uv run rental-finder run-once            # one rent cycle now (run-buy-once for buy)
uv run rental-finder install-scheduler   # run it automatically at your chosen times
```

## 2. Manual install (private repo, or you prefer to see each step)

```bash
git clone https://github.com/samuel2pb/real-state.git rental-finder
cd rental-finder
uv sync
uv run rental-finder init
```

Always run commands through `uv run ...`; uv manages the virtualenv for you.

## 3. Notion setup

1. Create an internal integration at <https://www.notion.so/profile/integrations> and copy its
   secret (starts with `ntn_` or `secret_`).
2. Create (or pick) a page that will hold the databases. Open it → `...` → **Connections** →
   add your integration. Without this step every call returns 404.
3. Run `uv run rental-finder init`. It asks for the token (hidden), the page URL or id, your
   modules and search, then offers to create one database per module (`Rent Listings — <city>`,
   `Buy Listings — <city>`) and writes their ids into `.env` as `NOTION_RENT_DB_ID` /
   `NOTION_BUY_DB_ID`.

Each database has a `Status` select (`available`, `gone`, `price-up`, `price-drop`), a
`Neighborhood` select seeded from your neighborhoods, price / area / distance columns, and
`FirstSeen` / `LastSeen` / `ListedAt` dates. Missing analytics columns are added on the next
run. To create a database later: `uv run python scripts/setup_notion.py --kind buy`.

## 4. Configuring your search

Everything lives in `.env` (written by `init`, template in `.env.example`, gitignored). The
**bold** ones are what you must set; the rest have sensible defaults. Buy filters are
independent from rent filters.

| Variable | Meaning |
|---|---|
| **`NOTION_TOKEN`**, **`NOTION_PARENT_PAGE_ID`** | Integration secret and the page that holds the databases |
| `NOTION_RENT_DB_ID`, `NOTION_BUY_DB_ID` | Filled in by `init` / `setup_notion.py` |
| **`MODULES_ENABLED`** | `rent`, `buy` or `rent,buy` |
| `RENT_CITY`, `RENT_STATE` | Default `São Paulo`, `SP` |
| **`RENT_NEIGHBORHOODS`** | Comma-separated, in preference order |
| **`RENT_PRICE_MIN`**, **`RENT_PRICE_MAX`** | Total monthly cost incl. condo + IPTU (BRL) |
| `RENT_BEDROOMS_MIN/MAX`, `RENT_BATHROOMS_MIN`, `RENT_SUITES_MIN`, `RENT_SQM_MIN`, `RENT_PARKING_MIN` | Unit constraints |
| `RENT_PETS_REQUIRED` | Only pet-friendly listings (default `true`) |
| `RENT_PROPERTY_TYPES`, `RENT_FURNISHED_ALLOWED` | `apartment,house,condo_house` (blank = any); allow furnished |
| `PRICE_DROP_MIN_PCT` | Ignore price drops smaller than this % (rent and buy) |
| `BUY_CITY`, `BUY_STATE` | Default `São Paulo`, `SP` |
| **`BUY_NEIGHBORHOODS`** | Blank = same as `RENT_NEIGHBORHOODS` |
| **`BUY_PRICE_MIN`**, **`BUY_PRICE_MAX`** | Sale price (BRL) |
| `BUY_CONDO_MAX` | Max monthly condo fee, `0` = no cap |
| `BUY_BEDROOMS_MIN/MAX`, `BUY_BATHROOMS_MIN`, `BUY_SUITES_MIN`, `BUY_SQM_MIN`, `BUY_PARKING_MIN` | Unit constraints |
| `BUY_PROPERTY_TYPES` | `apartment,house,condo_house`, blank = any |
| `BUY_PETS_REQUIRED`, `BUY_FURNISHED_ALLOWED`, `BUY_PREFILTER_ENABLED` | Buy toggles |
| **`WORK_ADDRESS`**, **`WORK_MAX_DISTANCE_KM`** | Distance is measured from here (straight line) |
| `WORK_LAT`, `WORK_LNG` | Optional; skips geocoding the address |
| `SCHEDULE_CRON_TIMES` | Run times as cron, comma-separated (default `0 9 * * *,0 18 * * *`) |
| `SCHEDULE_TZ` | Time zone for `rental-finder schedule` only |
| `POOL_WORKERS`, `HTTP_MIN_DELAY_SEC`, `HTTP_MAX_DELAY_SEC`, `HTTP_MAX_RETRIES` | Request pacing; raise the delays if you get blocked |
| `CONDO_*` | Building age lookup and age gate for buy (`CONDO_MAX_AGE_YEARS`, `CONDO_AGE_REQUIRE_KNOWN`, ...), see section 6 |

`.env.example` documents the advanced knobs (Notion pacing, geocoder, logging, cache).

## 5. Scheduling

`install-scheduler` registers the cycle with your OS scheduler, so nothing has to stay open:

```bash
uv run rental-finder install-scheduler                       # modules in MODULES_ENABLED, SCHEDULE_CRON_TIMES
uv run rental-finder install-scheduler --kind both --times "08:30,19:00"
uv run rental-finder install-scheduler --job recheck-gone --kind both --cron "0 14 * * SUN"
uv run rental-finder install-scheduler --dry-run             # print what it would do, change nothing
uv run rental-finder install-scheduler --uninstall           # remove what it created
```

| OS | What it creates |
|---|---|
| Windows | One Task Scheduler task per time, in the `RentalFinder` folder (`run-rent-0900`, ...). Runs while you are logged in. |
| macOS | `~/Library/LaunchAgents/com.rental-finder.<job>-<kind>.plist`, loaded with `launchctl bootstrap`. |
| Linux | `crontab` lines tagged `# rental-finder:<job>-<kind>`; re-running replaces them. |

Supported cron shapes are daily `M H * * *` and weekly `M H * * D` (`D` = `0-6` or `MON`..`SUN`).
Times are the machine's local time. Output is appended to `logs/<job>-<kind>.log` in the repo
(`logs/run-rent.log`, `logs/run-buy.log`). Re-run the command after changing the times.

Prefer a foreground process? `uv run rental-finder schedule` runs the same cycles from
`SCHEDULE_CRON_TIMES` in `SCHEDULE_TZ` until you stop it.

QuintoAndar rate-limits scrapers. The author runs every cycle behind a VPN. If you see repeated
403/429 in the log, do the same or raise `HTTP_MIN_DELAY_SEC` / `HTTP_MAX_DELAY_SEC`.

## 6. Building age (buy, optional)

Buy rows get `CondoName`, `BuiltYear`, `CondoAge`, `CondoSource` and `CondoConfidence`, and by
default **buildings older than 40 years, or with no trusted year, never reach Notion**.

- The name comes free from the QuintoAndar listing. The year is published on only ~10% of
  listings; the rest are resolved by the **[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI**
  (`claude -p` with web search), once per building, cached forever in `.cache/condos.json`.
  Cost is roughly $0.05–0.15 per building, capped per run by `CONDO_SEARCH_MAX_PER_RUN`.
- An answer is only accepted if the street number it read matches the listing's building;
  only `medium`/`high` confidence writes `BuiltYear` (`CONDO_SEARCH_MIN_CONFIDENCE`).
- **Age gate:** `CONDO_MAX_AGE_YEARS` (default 40, `0` disables). With `CONDO_AGE_REQUIRE_KNOWN`
  on (default), a listing whose year is unknown is rejected this run and retried next time.
- `condo_overrides.csv` (`key,name,built_year,block,why`) hand-fixes a year or, with `block=1`,
  forbids storing one for that building.

**No Claude Code?** Turn the lookup off with `CONDO_SEARCH_ENABLED=false` **and**
`CONDO_AGE_REQUIRE_KNOWN=false`; otherwise every listing without a published year is rejected.
Set `CONDO_ENRICH_ENABLED=false` to skip condo data entirely.

**Unattended runs:** an interactive Claude Code login expires, and a scheduled run then
resolves nothing. Create a long-lived token with `claude setup-token` and export
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) in the environment the scheduled task runs
under (a user environment variable on Windows, your shell profile for cron/launchd).

Background and measurements: [PLAN-CONDO-AGE.md](PLAN-CONDO-AGE.md).

## 7. Commands

All are `uv run rental-finder <command>`; `--help` on any of them lists its options.

| Command | What it does |
|---|---|
| `init` | Guided setup: write `.env` and (optionally) create the Notion databases |
| `doctor` | Check the install: `.env`, Notion token and DB ids, `uv` / `claude` on PATH |
| `run-once` | One full rent cycle (fetch + upsert + availability check) |
| `run-buy-once` | One full buy cycle |
| `install-scheduler` | Register the cycle with Task Scheduler / launchd / cron |
| `schedule` | Run the cron scheduler in the foreground (blocking) |
| `geo` | Geocode the work address and every neighborhood (warms the cache) |
| `recheck-gone` | Re-examine gone listings; promote price-up ones (`--kind rent\|buy\|both`) |
| `listed-at-backfill` | Fill `ListedAt` on rows the normal cycle never rewrites |
| `drop-source` | Trash every row of one `Source` (e.g. a retired source such as `zap`); dry run unless `--apply` |
| `condo-backfill` | Fill condo name / year / age / confidence on existing rows |
| `condo-purge-old` | Trash rows the age gate would refuse; dry run unless `--apply` |
| `condo-stats` | What the local condo registry knows |
| `condo-retry-misses` | Reopen buildings the lookup already failed on |
| `condo-lookup` | Debug the `claude -p` resolver on one building |
| `condo-search` | Debug the legacy SERP-snippet year search |
| `condo-invalidate` | Forget years from one source so they are resolved again |
| `condo-golden` | Re-resolve the hand-verified buildings and diff the years |

## 8. Architecture

```
cli.py               typer entry point (every command above)
config.py            pydantic-settings from .env; require_configured()
init_wizard.py       `init`: writes .env, calls notion_setup
doctor.py            `doctor` checks
scheduler_install.py `install-scheduler`: schtasks / launchd / crontab
scheduler.py         `schedule`: APScheduler in the foreground
pipeline.py          fetch -> filter -> enrich -> upsert; availability + price checks
sources/
  base.py            Source ABC (search_rent / search_buy, enrich, check_alive)
  quintoandar.py     QuintoAndar search + detail JSON, bbox-filtered
condos.py            building key, name cleanup, registry (.cache/condos.json)
condo_agent.py       build year via `claude -p` (prompts/condo_lookup.md)
condo_search.py      legacy SERP-snippet year search
http_client.py       curl-cffi session with browser TLS impersonation, pacing, retry
notion_setup.py      creates the databases
notion_store.py      upsert / mark gone / list via data_sources.query
geo.py               Nominatim geocoding + haversine distance, cached
models.py            Listing dataclass
```

Guarded in code; do not change without a test:

- QuintoAndar is searched by **bounding box** (each neighborhood resolved once via Nominatim
  into `.cache/bboxes.json`), not by neighborhood name.
- Notion's `status` property type cannot be created through the API, so `Status` is a
  **`select`** with `available` / `gone` / `price-up` / `price-drop`.
- notion-client 3.x queries with **`data_sources.query(data_source_id=...)`**, not
  `databases.query`.
- QuintoAndar publishes one unlabelled `area` (it can include parking), so its rows say
  **`SqmBasis = unspecified`**; R$/m² on those rows is a floor, not an exact value.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `rental-finder is not configured ... missing X` | Run `uv run rental-finder init` (or fill `X` in `.env`), then `doctor`. |
| Repeated 403 / 429 from QuintoAndar | You are rate-limited. Connect a VPN, raise `HTTP_MIN_DELAY_SEC` / `HTTP_MAX_DELAY_SEC`, lower `POOL_WORKERS`. Retries back off on their own. |
| Notion 404 / `object_not_found` | The integration is not connected to the parent page (section 3, step 2). |
| Notion 400 on a select option | The database is missing an option a filter names (usually an old `Status`). Run a cycle once: missing `Status` options and columns are added automatically. A brand-new database from `init` never has this. |
| Scheduled task never fires | The machine was asleep or logged out at that time (Windows tasks need you logged in; launchd runs missed jobs at wake, cron does not). Check `logs/<job>-<kind>.log` and `install-scheduler --dry-run`. |
| Task fires but every request fails | The VPN was not connected when it ran. |
| Buy cycle keeps zero rows | Probably the age gate with no Claude Code available; see section 6. |
