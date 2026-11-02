from __future__ import annotations

import logging
import subprocess

import structlog
import typer

from .config import settings
from .pipeline import run_rent_cycle, recheck_gone

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _init_logging() -> None:
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
    )


@app.command("run-once")
def run_once() -> None:
    """Run one full cycle (fetch + upsert + availability check)."""
    _init_logging()
    stats = run_rent_cycle()
    typer.echo(stats)


@app.command("run-buy-once")
def run_buy_once() -> None:
    """Run one full buy cycle (fetch + upsert + availability check)."""
    _init_logging()
    from .pipeline import run_buy_cycle
    stats = run_buy_cycle()
    typer.echo(stats)


@app.command("recheck-gone")
def recheck_gone_cmd(
    kind: str = typer.Option("both", help="'rent', 'buy', or 'both'"),
) -> None:
    """Re-examine gone listings; promote price-up ones. Occasional backfill only."""
    _init_logging()
    if kind in ("rent", "both"):
        stats = recheck_gone(kind="rent")
        typer.echo({"kind": "rent", **stats})
    if kind in ("buy", "both"):
        stats = recheck_gone(kind="buy")
        typer.echo({"kind": "buy", **stats})


@app.command("listed-at-backfill")
def listed_at_backfill_cmd(
    kind: str = typer.Option("both", help="'rent', 'buy', or 'both'"),
    include_gone: bool = typer.Option(
        True, help="Also backfill rows already marked gone"
    ),
) -> None:
    """Fill ListedAt on rows the normal cycle never rewrites (gone / price-*)."""
    _init_logging()
    from .pipeline import backfill_listed_at

    for k in ("rent", "buy") if kind == "both" else (kind,):
        stats = backfill_listed_at(kind=k, include_gone=include_gone)
        typer.echo({"kind": k, **stats})


@app.command("condo-backfill")
def condo_backfill_cmd(
    kind: str = typer.Option("buy", help="'buy' or 'rent'"),
    max_searches: int = typer.Option(
        0, help="Cap lookups this run (0 = use CONDO_SEARCH_MAX_PER_RUN)"
    ),
    neighborhoods: str = typer.Option(
        "", help="Comma-separated bairros; empty = every bairro in the base"
    ),
    bedrooms_min: int = typer.Option(0, help="Skip rows below this bedroom count"),
    only_missing: bool = typer.Option(
        False, help="Skip rows that already carry a BuiltYear"
    ),
    refresh: bool = typer.Option(
        False,
        help="Ignore the BuiltYear already on the row and resolve it again. "
        "Use after condo-invalidate, or the retired value is just re-stamped.",
    ),
) -> None:
    """Fill CondoName/BuiltYear/CondoAge/CondoConfidence on existing Notion rows."""
    _init_logging()
    from .pipeline import backfill_condos

    stats = backfill_condos(
        kind=kind,
        max_searches=max_searches or None,
        neighborhoods=[n for n in neighborhoods.split(",") if n.strip()],
        bedrooms_min=bedrooms_min,
        only_missing=only_missing,
        refresh=refresh,
    )
    typer.echo({"kind": kind, **stats})


@app.command("condo-purge-old")
def condo_purge_old_cmd(
    kind: str = typer.Option("buy", help="'buy' or 'rent'"),
    max_age: int = typer.Option(
        0, help="Age cap in years (0 = use CONDO_MAX_AGE_YEARS)"
    ),
    include_gone: bool = typer.Option(True, help="Also sweep rows marked gone"),
    resolve: bool = typer.Option(
        False,
        help="Run the agent on rows with no year at all. Costs money — a dry "
        "run first will say how many rows that is.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually trash the rows (default: report only)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Trash rows the age gate would refuse. Dry run unless --apply.

    Past the age cap, or (CONDO_AGE_REQUIRE_KNOWN) with a year that can no
    longer be found. Rows go to Notion's trash, recoverable for 30 days.
    """
    _init_logging()
    from .pipeline import purge_old_condos

    if apply and not yes:
        typer.confirm(
            f"Trash every {kind} row older than "
            f"{max_age or settings.condo_max_age_years} years"
            + (
                " or with no known build year?"
                if settings.condo_age_require_known
                else "?"
            ),
            abort=True,
        )
    stats = purge_old_condos(
        kind=kind,
        max_age=max_age or None,
        include_gone=include_gone,
        apply=apply,
        resolve=resolve,
    )
    typer.echo({"kind": kind, "apply": apply, **stats})


@app.command("condo-stats")
def condo_stats_cmd() -> None:
    """Print what the local condo registry knows."""
    _init_logging()
    from .condos import registry

    typer.echo(registry().stats())


@app.command("condo-retry-misses")
def condo_retry_misses_cmd(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Reopen buildings the search already failed on.

    Misses are parked for good by default. Only worth reopening after the
    extractor or the query template changed — each reopened building costs a
    search credit again on the next run.
    """
    _init_logging()
    from .condos import registry

    reg = registry()
    parked = reg.stats()["parked_misses"]
    if not parked:
        typer.echo("no parked misses")
        return
    if not yes:
        typer.confirm(
            f"Reopen {parked} parked buildings? They will be searched again.",
            abort=True,
        )
    typer.echo({"reopened": reg.clear_misses()})


@app.command("condo-search")
def condo_search_cmd(
    name: str = typer.Argument(..., help="Condo name, e.g. 'Mustique'"),
    street: str = typer.Argument(..., help="Street, e.g. 'Rua Arandu'"),
    number: str = typer.Argument(..., help="Street number, e.g. '222'"),
    neighborhood: str = typer.Option("", help="Neighborhood, sharpens the query"),
) -> None:
    """One-shot debug of the legacy SERP-snippet year search."""
    _init_logging()
    from .condo_search import build_query, search_year

    typer.echo(build_query(name, street, number, neighborhood))
    hit = search_year(
        name=name, street=street, number=number, neighborhood=neighborhood
    )
    typer.echo(hit if hit else "no year found")


@app.command("condo-lookup")
def condo_lookup_cmd(
    street: str = typer.Argument(..., help="Street, e.g. 'Rua Jose Antonio Coelho'"),
    number: str = typer.Argument(..., help="Street number, e.g. '603'"),
    name: str = typer.Option("", help="Building name, when the listing has one"),
    neighborhood: str = typer.Option("", help="Neighborhood, sharpens the search"),
    sqm: float = typer.Option(0, help="Unit size, helps identify an unnamed building"),
    condo_fee: float = typer.Option(0, help="Monthly condo fee, same purpose"),
    bedrooms: int = typer.Option(0),
    suites: int = typer.Option(0),
    parking: int = typer.Option(0),
    show_prompt: bool = typer.Option(False, help="Print the prompt and exit"),
) -> None:
    """One-shot debug of the `claude -p` resolver for a single building."""
    _init_logging()
    from .condo_agent import build_prompt, lookup_condo

    hints = {
        "sqm": sqm,
        "condo_fee": condo_fee,
        "bedrooms": bedrooms,
        "suites": suites,
        "parking": parking,
    }
    if show_prompt:
        typer.echo(
            build_prompt(
                name=name,
                street=street,
                number=number,
                neighborhood=neighborhood,
                hints=hints,
            )
        )
        return
    finding = lookup_condo(
        name=name,
        street=street,
        number=number,
        neighborhood=neighborhood,
        hints=hints,
    )
    typer.echo(finding if finding else "resolver unreachable")


@app.command("condo-invalidate")
def condo_invalidate_cmd(
    source: str = typer.Option("search", help="Year source to drop: search|claude|quintoandar"),
    max_confidence: str = typer.Option(
        "", help="Only drop values at or below this confidence (e.g. 'medium')"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Forget years that came from one source, so they are resolved again.

    The SERP tier scored 328 years as 'medium' with the same rule that recorded
    a 1977 building as 2023 off a neighbourhood index page. None of them were
    checked against the address they came from, so they are all suspect.
    """
    _init_logging()
    from .condos import registry

    reg = registry()
    if not yes:
        typer.confirm(
            f"Drop every year with source={source!r}"
            + (f" and confidence <= {max_confidence!r}" if max_confidence else "")
            + "? They will be resolved again on the next run.",
            abort=True,
        )
    typer.echo({"cleared": reg.clear_source(source, max_confidence=max_confidence)})


@app.command("condo-golden")
def condo_golden_cmd(
    path: str = typer.Option(
        "tests/fixtures/condo_golden.csv", help="CSV of key,name,built_year"
    ),
    use_cache: bool = typer.Option(
        False, help="Answer from the registry instead of calling the resolver"
    ),
) -> None:
    """Run the resolver over the hand-verified buildings and diff the years.

    The expected rows are the ones checked by hand in the 2026-09-05 handoff.
    Rows with an empty built_year are negative cases: the resolver must return
    no year for them, because no public source states one.
    """
    _init_logging()
    import csv as _csv

    from .condo_agent import lookup_condo
    from .condos import registry, split_key

    reg = registry()
    rows = list(_csv.DictReader(open(path, encoding="utf-8")))
    ok = bad = 0
    for row in rows:
        key = (row.get("key") or "").strip()
        want = (row.get("built_year") or "").strip()
        street, number = split_key(key)
        info = reg.lookup(key)
        if info is not None and info.blocked:
            # An override says no resolver may answer for this building. Report
            # it rather than skipping it: a silent pass would make the golden set
            # look green for the wrong reason.
            got, conf = None, "blocked"
        elif use_cache:
            got, conf = (info.built_year if info else None), (
                info.year_confidence if info else ""
            )
        else:
            finding = lookup_condo(
                name=(row.get("name") or "").strip(),
                street=street,
                number=number,
                neighborhood=(row.get("neighborhood") or "").strip(),
                # The same unit facts the pipeline sends. Without them the run
                # cannot reproduce a rejection that was made on unit profile —
                # City Park is the only building at Rua Humberto I 928, and only
                # its fee and room counts say it is not the one.
                hints={
                    k: float(row[k])
                    for k in ("sqm", "condo_fee", "bedrooms", "suites", "parking")
                    if (row.get(k) or "").strip()
                },
            )
            got, conf = (
                (finding.built_year, finding.confidence) if finding else (None, "unreachable")
            )
        expected = int(want) if want else None
        match = got == expected
        ok, bad = (ok + 1, bad) if match else (ok, bad + 1)
        typer.echo(
            f"{'OK ' if match else 'BAD'} {key:<48} want={want or 'null':<6} "
            f"got={got or 'null':<6} conf={conf}"
        )
    typer.echo({"rows": len(rows), "match": ok, "mismatch": bad})


@app.command("schedule")
def schedule() -> None:
    """Start the cron scheduler (blocking)."""
    settings.require_configured()
    _init_logging()
    from .scheduler import start
    start()


@app.command("geo")
def geo() -> None:
    """Geocode work address + all neighborhoods (warms cache)."""
    settings.require_configured()
    _init_logging()
    from .geo import work_coords, neighborhood_bbox
    typer.echo(f"work: {work_coords()}")
    for n in settings.neighborhoods_list:
        typer.echo(f"{n}: {neighborhood_bbox(n)}")


@app.command("drop-source")
def drop_source_cmd(
    source: str = typer.Option(..., help="Source value to remove, e.g. 'zap'"),
    kind: str = typer.Option(..., help="'rent' or 'buy'"),
    apply: bool = typer.Option(
        False, "--apply", help="Actually trash the rows (default: report only)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Trash every row of one Source (e.g. the retired 'zap'). Dry run unless --apply.

    Rows go to Notion's trash, recoverable for 30 days.
    """
    if kind not in ("rent", "buy"):
        raise typer.BadParameter("kind must be 'rent' or 'buy'")
    _init_logging()
    from .pipeline import drop_source

    if apply and not yes:
        typer.confirm(
            f"Trash every {kind} row with Source={source!r}, whatever its status?",
            abort=True,
        )
    stats = drop_source(kind=kind, source=source, apply=apply)
    typer.echo({"kind": kind, "source": source, "apply": apply, **stats})


@app.command("init")
def init_cmd(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .env"),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Take every value from the options below (or the .env.example "
        "default); never prompt",
    ),
    create_dbs: bool | None = typer.Option(
        None,
        "--create-dbs/--no-create-dbs",
        help="Create the Notion databases after writing .env "
        "(default: ask; skipped when non-interactive)",
    ),
    notion_token: str | None = typer.Option(None, help="NOTION_TOKEN"),
    notion_parent_page_id: str | None = typer.Option(
        None, help="NOTION_PARENT_PAGE_ID (id or page URL)"
    ),
    modules: str | None = typer.Option(None, help="MODULES_ENABLED: rent | buy | rent,buy"),
    rent_neighborhoods: str | None = typer.Option(None, help="RENT_NEIGHBORHOODS"),
    rent_price_min: int | None = typer.Option(None, help="RENT_PRICE_MIN"),
    rent_price_max: int | None = typer.Option(None, help="RENT_PRICE_MAX"),
    rent_bedrooms_min: int | None = typer.Option(None, help="RENT_BEDROOMS_MIN"),
    rent_bedrooms_max: int | None = typer.Option(None, help="RENT_BEDROOMS_MAX"),
    buy_neighborhoods: str | None = typer.Option(None, help="BUY_NEIGHBORHOODS"),
    buy_price_min: int | None = typer.Option(None, help="BUY_PRICE_MIN"),
    buy_price_max: int | None = typer.Option(None, help="BUY_PRICE_MAX"),
    work_address: str | None = typer.Option(None, help="WORK_ADDRESS"),
    work_max_distance_km: float | None = typer.Option(None, help="WORK_MAX_DISTANCE_KM"),
    schedule_times: str | None = typer.Option(
        None, help="Run times as HH:MM, comma-separated, e.g. 09:00,18:00"
    ),
) -> None:
    """Guided setup: write .env and (optionally) create the Notion databases."""
    from .init_wizard import run_init

    given = {
        "NOTION_TOKEN": notion_token,
        "NOTION_PARENT_PAGE_ID": notion_parent_page_id,
        "MODULES_ENABLED": modules,
        "RENT_NEIGHBORHOODS": rent_neighborhoods,
        "RENT_PRICE_MIN": rent_price_min,
        "RENT_PRICE_MAX": rent_price_max,
        "RENT_BEDROOMS_MIN": rent_bedrooms_min,
        "RENT_BEDROOMS_MAX": rent_bedrooms_max,
        "BUY_NEIGHBORHOODS": buy_neighborhoods,
        "BUY_PRICE_MIN": buy_price_min,
        "BUY_PRICE_MAX": buy_price_max,
        "WORK_ADDRESS": work_address,
        "WORK_MAX_DISTANCE_KM": work_max_distance_km,
        "SCHEDULE_TIMES": schedule_times,
    }
    given = {k: (None if v is None else str(v)) for k, v in given.items()}
    try:
        run_init(
            given,
            force=force,
            interactive=not non_interactive,
            create_dbs=create_dbs,
        )
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        typer.echo(f"init: {e}", err=True)
        raise typer.Exit(1)
    typer.echo("Next: `uv run rental-finder doctor`, then `uv run rental-finder run-once`.")


@app.command("doctor")
def doctor_cmd() -> None:
    """Check the install: .env, Notion token and DB ids, uv / claude on PATH."""
    from .doctor import has_failures, run_checks

    checks = run_checks()
    width = max(len(name) for name, _, _ in checks)
    for name, status, detail in checks:
        typer.echo(f"{status:<4}  {name:<{width}}  {detail}")
    if has_failures(checks):
        raise typer.Exit(1)


@app.command("install-scheduler")
def install_scheduler_cmd(
    kind: str | None = typer.Option(
        None, help="rent | buy | both (default: the modules in MODULES_ENABLED)"
    ),
    job: str = typer.Option(
        "run", help="run (the normal cycle) | recheck-gone (pair with a weekly cron)"
    ),
    times: str | None = typer.Option(
        None,
        help="Daily run times as HH:MM, comma-separated, e.g. 09:00,18:00 "
        "(default: SCHEDULE_CRON_TIMES)",
    ),
    uninstall: bool = typer.Option(
        False, "--uninstall", help="Remove the entries this command created"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the commands and files; change nothing"
    ),
) -> None:
    """Register the cycle with the OS scheduler (Task Scheduler / launchd / cron)."""
    from .scheduler_install import (
        SchedulerError,
        install,
        parse_crons,
        resolve_kinds,
        triggers_from_times,
    )

    try:
        kinds = resolve_kinds(kind, settings.modules_list)
        triggers = (
            triggers_from_times(times) if times else parse_crons(settings.cron_list)
        )
        done = install(
            kinds=kinds, job=job, triggers=triggers, uninstall=uninstall, dry_run=dry_run
        )
    except SchedulerError as e:
        typer.echo(f"install-scheduler: {e}", err=True)
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        typer.echo(f"install-scheduler: {' '.join(e.cmd)} failed: {detail}", err=True)
        raise typer.Exit(1)
    prefix = "(dry run) would have: " if dry_run else ""
    for line in done:
        typer.echo(f"{prefix}{line}")


def main() -> None:
    from .config import NotConfiguredError

    try:
        app()
    except NotConfiguredError as e:
        typer.echo(str(e), err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
