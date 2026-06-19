from __future__ import annotations

import logging

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


@app.command("schedule")
def schedule() -> None:
    """Start the cron scheduler (blocking)."""
    _init_logging()
    from .scheduler import start
    start()


@app.command("geo")
def geo() -> None:
    """Geocode work address + all neighborhoods (warms cache)."""
    _init_logging()
    from .geo import work_coords, neighborhood_bbox
    typer.echo(f"work: {work_coords()}")
    for n in settings.neighborhoods_list:
        typer.echo(f"{n}: {neighborhood_bbox(n)}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
