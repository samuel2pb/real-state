# PLAN — QuintoAndar-only, shareable install

Base: `feat/condo-agent-tier` (e98641f). Branch: `claude/zap-imoveis-refactor-a6a73d`.

Goal: remove every ZapImóveis code path, keep QuintoAndar as the single source,
keep the `rent` / `buy` module names exactly as they are (CLI commands, `RENT_*` /
`BUY_*` env prefixes, `Listing.mode`, Notion DB kinds, `run_rent_cycle` /
`run_buy_cycle`), and make the project installable and configurable by someone
who has never seen it: one-line installer, guided `.env` setup, and a scheduler
installer for Windows / macOS / Linux.

Assumptions (stated, not asked):
- "classes de rent e buy" = the two modules keep their names; no renaming.
- The SERP tier (`condo_search.py`) and the `claude -p` tier stay. Only Zap goes.
- Existing Notion rows with `Source = zap` are left alone by the cycles; a
  one-off `drop-source` command trashes them on request.
- The repo is public at `github.com/samuel2pb/real-state`; installers fetch
  from `raw.githubusercontent.com`. If it stays private, the README also
  documents the clone-then-run path.

---

## Phase 1 — Remove ZapImóveis

Delete:
- `src/rental_finder/sources/zapimoveis.py`
- `scripts/create_rent_db.json` (hard-coded personal page id; superseded by `setup_notion.py`)
- `scripts/_probe_*.py` (zap-era probes, no longer runnable)
- `PLAN-SALE.md` zap sections stay as history; do not edit old plans except `PLAN.md`/`PLAN-SALE.md` get a one-line banner "ZapImóveis removed on 2026-11-02, see PLAN-QUINTOANDAR-ONLY.md".

Edit:
- `sources/__init__.py`: drop `ZapSource`; `enabled_sources(mode)` returns `[QuintoAndarSource()]` for both modes; `__all__ = ["Source", "QuintoAndarSource"]`.
- `config.py`: delete `source_zapimoveis_enabled`, `source_quintoandar_enabled`, `source_zapimoveis_buy_enabled`, `source_quintoandar_buy_enabled`, `http_playwright_fallback` (never read anywhere). Align `http_impersonate` default with `.env.example` (`chrome124` in the example vs `chrome146` in code — pick `chrome124`, which `curl_cffi` 0.15 ships, and make both agree).
- `.env.example`: drop the `# ----- Sources -----` blocks and `HTTP_PLAYWRIGHT_FALLBACK`. Replace personal defaults with neutral placeholders: `WORK_ADDRESS=Av. Paulista, 1000 - Bela Vista, São Paulo - SP`. Group vars in the order a newcomer needs them: Notion → modules → rent filters → buy filters → work location → schedule → HTTP → condo → advanced.
- `scripts/setup_notion.py` + `notion_store.py`: `Source` select has only `quintoandar`. `NEIGHBORHOOD_OPTIONS` must be built from `settings.neighborhoods_list` / `buy_neighborhoods_list` (no hard-coded list). Keep the script, but it is also exposed as `rental-finder setup-notion --kind rent|buy` (Phase 3 wires it).
- `models.py`, `notion_store.py`, `condos.py`, `pipeline.py`, `quintoandar.py`: fix comments that mention zap (`source: str  # "quintoandar"`, IPTU note, `SqmBasis` docstring). `condo_search.py` keeps `zapimoveis.com.br` in its *search-domain* list — that is a data source for years, not the scraper.
- `pyproject.toml`: description → "QuintoAndar rent/buy listings → Notion, filtered to your criteria."
- Tests: `test_filters.py`, `test_price_drop.py` use `source="quintoandar"`; `test_area_basis.py` and `test_listed_at.py` lose their `ZapSource` cases (keep the QuintoAndar ones and the model-level ones). `test_config.py` must `pytest.skip` when `.env` is missing (fresh clone runs the suite green).
- Add `rental-finder drop-source --source zap --kind rent|buy [--apply]`: lists rows with that `Source`, trashes with `NotionStore.archive_page` on `--apply`; dry run default. Reuse the `_list_by_status`/pager pattern already in `notion_store.py`.

Done when: `git grep -i zap -- src tests scripts pyproject.toml` returns only the `condo_search.py` domain line and the `drop-source` command; `uv run pytest` green.

## Phase 2 — Configuration for a stranger

- `rental-finder init`: interactive wizard (typer prompts) that writes `.env` from `.env.example`: Notion token, parent page id, modules (`rent`, `buy`, both), neighborhoods, price ranges, bedrooms, work address + max distance, schedule times. Then (optional, prompt) creates the Notion DBs via the `setup_notion` logic and writes `NOTION_RENT_DB_ID` / `NOTION_BUY_DB_ID` back into `.env`. `--non-interactive` + flags for scripts. Never prints the token back.
- `config.py` must import without a `.env` so `init` and `--help` work before setup: make `notion_token`, `notion_parent_page_id`, `rent_neighborhoods`, `rent_price_min/max`, `work_address` default to `""`/`0` and add `settings.require_configured()` that raises a clear message ("run `rental-finder init`") — called at the top of `run_rent_cycle`, `run_buy_cycle`, `recheck_gone`, `schedule`, `geo`.
- `rental-finder doctor`: checks `.env` present, token shape, DB ids set, Notion reachable, `claude` on PATH when `CONDO_SEARCH_PROVIDER=claude`, `uv` on PATH; prints pass/fail per item.

## Phase 3 — Scheduler installer

New module `src/rental_finder/scheduler_install.py` + command
`rental-finder install-scheduler [--kind rent|buy|both] [--job run|recheck-gone] [--times "09:00,18:00"] [--uninstall] [--dry-run]`.

- Reads defaults from `SCHEDULE_CRON_TIMES` (5-field cron, comma-separated). Supports daily `M H * * *` and weekly `M H * * D`; anything else → error listing what is supported.
- Resolves `uv` absolute path (`shutil.which`) and repo root (`config.ROOT`). Command run by the scheduler is always `uv run --directory <ROOT> rental-finder <cmd>` with stdout/stderr appended to `<ROOT>/logs/<job>-<kind>.log`.
  - `run` + `rent` → `run-once`; `run` + `buy` → `run-buy-once`; `recheck-gone` → `recheck-gone --kind <kind>` (reserved for the weekly recheck another session is adding).
- Windows: one task per time, `schtasks /Create /F /TN "RentalFinder\<job>-<kind>-<HHMM>" /SC DAILY|WEEKLY [/D MON] /ST HH:MM /TR "cmd /c \"...\" >> log 2>&1"`. `--uninstall` → `schtasks /Delete /F` on every task under the `RentalFinder\` folder that matches.
- macOS: `~/Library/LaunchAgents/com.rental-finder.<job>-<kind>.plist` with one `StartCalendarInterval` dict per time; `launchctl bootout gui/$UID <plist>` (ignore failure) then `launchctl bootstrap gui/$UID <plist>`. Uninstall → bootout + delete.
- Linux: crontab lines tagged `# rental-finder:<job>-<kind>`; install replaces lines with that tag, uninstall removes them. Use `crontab -l` / `crontab -`.
- `--dry-run` prints the exact commands / file contents and touches nothing.
- Delete `scripts/rental-finder-rent.xml`, `scripts/rental-finder-buy.xml`, `scripts/run_daily.cmd`, `scripts/run_buy_daily.cmd`.
- Tests: `tests/test_scheduler_install.py` — cron → trigger parsing, command string for each OS with `platform.system` monkeypatched, crontab line replace/remove is idempotent. No real scheduler calls in tests.

## Phase 4 — One-line installer

- `scripts/install.sh` (bash, macOS + Linux): `set -euo pipefail`; install `uv` if missing (official `astral.sh/uv/install.sh`); clone `https://github.com/samuel2pb/real-state.git` into `${RENTAL_FINDER_HOME:-$HOME/rental-finder}` or `git pull` if present; `uv sync`; then `uv run rental-finder init` unless `--no-init`; prints next steps (`install-scheduler`).
- `scripts/install.ps1` (Windows PowerShell 5.1 compatible — no `&&`, no ternary): same flow; `uv` via `irm https://astral.sh/uv/install.ps1 | iex`; default home `$env:USERPROFILE\rental-finder`.
- Both accept `--ref <branch>` / `-Ref` to install a branch other than `main`.
- README one-liners:
  - `curl -fsSL https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.sh | bash`
  - `irm https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.ps1 | iex`

## Phase 5 — README rewrite

Audience: a friend who will run this on their own machine. Structure:
1. What it does (QuintoAndar → filters → Notion, rent and buy), 3 lines.
2. Quick start: one-liner per OS → `rental-finder init` → `rental-finder doctor` → `rental-finder run-once` → `rental-finder install-scheduler`.
3. Manual install (clone + `uv sync`) for a private repo.
4. Notion setup (integration, share page, what `init` creates).
5. Configuring your search: table of `RENT_*` and `BUY_*`, work location, schedule, with the few you must change in bold.
6. Scheduling: what `install-scheduler` creates on each OS, where logs go, `--uninstall`, and the foreground alternative `rental-finder schedule`. Note the VPN caveat (QuintoAndar rate-limits; the author runs it behind a VPN).
7. Condo age tier (buy): condensed from the current README; make clear it is optional, needs Claude Code CLI, and how to turn it off (`CONDO_SEARCH_ENABLED=false`, `CONDO_AGE_REQUIRE_KNOWN=false`).
8. Commands reference (every CLI command, one line each).
9. Architecture (module map, QuintoAndar-only) + the guarded anti-patterns that still apply (bbox, Notion `select` status, `data_sources.query`).
10. Troubleshooting: 403/429, Notion 400 on select options, missing `.env`.

Remove every Zap line. Keep it under ~250 lines; details stay in `PLAN-CONDO-AGE.md`.

## Verification

- `uv run pytest` green with and without `.env`.
- `uv run rental-finder --help` works on a fresh clone with no `.env`.
- `uv run rental-finder install-scheduler --dry-run --kind both` prints Windows commands here; monkeypatched tests cover mac/linux.
- `git grep -n -i zap -- src tests scripts pyproject.toml README.md` → only `condo_search.py` domain list + `drop-source`.
- `bash -n scripts/install.sh`; `powershell -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/install.ps1 -Raw))"` parses.
