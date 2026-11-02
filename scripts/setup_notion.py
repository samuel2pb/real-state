"""Create the Notion DB for a module (rent / buy) inside the parent page.

Thin wrapper over `rental_finder.notion_setup.create_database`; `rental-finder
init` runs the same logic and writes the id into .env for you.

Usage:
    uv run python scripts/setup_notion.py --kind rent
    uv run python scripts/setup_notion.py --kind buy
"""

from __future__ import annotations

import typer

from rental_finder.notion_setup import KINDS, create_database


def main(kind: str = "rent") -> None:
    if kind not in KINDS:
        raise typer.BadParameter(f"unknown kind {kind!r}; must be one of {list(KINDS)}")
    out = create_database(kind)
    typer.echo(f"Created {kind} DB: {out['id']}")
    typer.echo(f"URL: {out['url']}")
    typer.echo(f"Properties applied: {out['properties_applied']}")
    typer.echo(f"-> paste into .env as NOTION_{kind.upper()}_DB_ID")


if __name__ == "__main__":
    typer.run(main)
