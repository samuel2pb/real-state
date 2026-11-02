"""`rental-finder init`: write .env from .env.example, then optionally create
the Notion databases.

`settings` is built at import time from whatever .env existed then (possibly
none), so nothing here reads it: the wizard works off `config.ROOT` and the
values it collects. The text helpers are pure so they can be unit tested.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import typer

from .config import ROOT

_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_PAGE_ID_RE = re.compile(r"([0-9a-fA-F]{32})(?:[?#].*)?$")

MODULE_CHOICES = {"rent": "rent", "buy": "buy", "rent,buy": "rent,buy", "both": "rent,buy"}


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """KEY=value pairs of a dotenv text. Comments and blank lines are ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _clean(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def set_env_values(text: str, values: dict[str, str]) -> str:
    """Return `text` with each KEY's value replaced in place.

    Comments, blank lines and order are kept. A key that appears more than once
    has every occurrence set. Keys the text does not have are appended at the
    end, so nothing the caller asked for is silently dropped.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    done: set[str] = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if m and m.group(1) in values:
            key = m.group(1)
            lines[i] = f"{key}={_clean(values[key])}"
            done.add(key)
    extra = [k for k in values if k not in done]
    if extra:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{k}={_clean(values[k])}" for k in extra)
    return newline.join(lines) + newline


def times_to_cron(times: str) -> str:
    """'09:00,18:00' -> '0 9 * * *,0 18 * * *' (daily, one cron per time)."""
    out: list[str] = []
    for raw in times.split(","):
        t = raw.strip()
        if not t:
            continue
        m = _TIME_RE.match(t)
        if not m:
            raise ValueError(f"bad time {t!r}; use HH:MM, e.g. 09:00")
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"bad time {t!r}; hour 0-23, minute 0-59")
        out.append(f"{minute} {hour} * * *")
    if not out:
        raise ValueError("at least one time is required, e.g. 09:00,18:00")
    return ",".join(out)


def cron_to_times(cron: str) -> str:
    """Best-effort inverse of times_to_cron, for showing a default. '' if not daily."""
    out: list[str] = []
    for expr in cron.split(","):
        parts = expr.split()
        if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
            return ""
        if not (parts[0].isdigit() and parts[1].isdigit()):
            return ""
        out.append(f"{int(parts[1]):02d}:{int(parts[0]):02d}")
    return ",".join(out)


def normalize_modules(value: str) -> str:
    key = ",".join(p.strip().lower() for p in value.split(",") if p.strip())
    if key == "buy,rent":
        key = "rent,buy"
    if key not in MODULE_CHOICES:
        raise ValueError(f"modules must be one of rent, buy, rent,buy (got {value!r})")
    return MODULE_CHOICES[key]


def normalize_page_id(value: str) -> str:
    """Accept a bare id (with or without dashes) or a full Notion page URL."""
    v = value.strip()
    m = _PAGE_ID_RE.search(v.replace("-", ""))
    if not m:
        return v
    h = m.group(1).lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------

# Keys the wizard owns, in prompt order.
RENT_KEYS = (
    "RENT_NEIGHBORHOODS",
    "RENT_PRICE_MIN",
    "RENT_PRICE_MAX",
    "RENT_BEDROOMS_MIN",
    "RENT_BEDROOMS_MAX",
)
BUY_KEYS = ("BUY_NEIGHBORHOODS", "BUY_PRICE_MIN", "BUY_PRICE_MAX")

_LABELS = {
    "NOTION_PARENT_PAGE_ID": "Notion parent page id or URL",
    "MODULES_ENABLED": "Modules to run (rent / buy / rent,buy)",
    "RENT_NEIGHBORHOODS": "Rent neighborhoods (comma-separated, preference order)",
    "RENT_PRICE_MIN": "Rent: min total monthly price incl. condo + IPTU (BRL)",
    "RENT_PRICE_MAX": "Rent: max total monthly price incl. condo + IPTU (BRL)",
    "RENT_BEDROOMS_MIN": "Bedrooms min",
    "RENT_BEDROOMS_MAX": "Bedrooms max",
    "BUY_NEIGHBORHOODS": "Buy neighborhoods (blank = same as rent)",
    "BUY_PRICE_MIN": "Buy: min sale price (BRL)",
    "BUY_PRICE_MAX": "Buy: max sale price (BRL)",
    "WORK_ADDRESS": "Work address (distance is measured from here)",
    "WORK_MAX_DISTANCE_KM": "Max distance from work (km)",
}

_INT_KEYS = {
    "RENT_PRICE_MIN",
    "RENT_PRICE_MAX",
    "RENT_BEDROOMS_MIN",
    "RENT_BEDROOMS_MAX",
    "BUY_PRICE_MIN",
    "BUY_PRICE_MAX",
}


def _validate(key: str, value: str) -> str:
    value = _clean(value)
    if key in _INT_KEYS:
        if not value.isdigit():
            raise ValueError(f"{key} must be a whole number (got {value!r})")
    elif key == "WORK_MAX_DISTANCE_KM":
        try:
            if float(value) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(f"{key} must be a positive number (got {value!r})") from None
    elif key == "MODULES_ENABLED":
        value = normalize_modules(value)
    elif key == "NOTION_PARENT_PAGE_ID" and value:
        value = normalize_page_id(value)
    return value


def _ask(key: str, default: str, prompt: Callable[..., str]) -> str:
    while True:
        raw = prompt(_LABELS.get(key, key), default=default)
        try:
            return _validate(key, raw)
        except ValueError as e:
            typer.echo(f"  {e}")


def collect_values(
    given: dict[str, str | None],
    defaults: dict[str, str],
    *,
    interactive: bool,
    prompt: Callable[..., str] = typer.prompt,
) -> dict[str, str]:
    """Resolve every wizard key from the CLI options, the prompts or the example.

    `given` holds the CLI options (None = not passed); a passed option is never
    prompted for. `SCHEDULE_TIMES` is the human form ("09:00,18:00") and is
    converted to SCHEDULE_CRON_TIMES here.
    """
    out: dict[str, str] = {}

    def pick(key: str) -> None:
        passed = given.get(key)
        default = defaults.get(key, "") if passed is None else passed
        if interactive and passed is None:
            out[key] = _ask(key, default, prompt)
        else:
            out[key] = _validate(key, default)

    # Token: hidden, never echoed back, never defaulted from the example.
    token = given.get("NOTION_TOKEN")
    if token is None and interactive:
        token = prompt("Notion integration token (ntn_... / secret_...)", hide_input=True)
    out["NOTION_TOKEN"] = _clean(token or "")

    pick("NOTION_PARENT_PAGE_ID")
    pick("MODULES_ENABLED")
    for k in RENT_KEYS:
        pick(k)
    if "buy" in out["MODULES_ENABLED"].split(","):
        for k in BUY_KEYS:
            pick(k)
    pick("WORK_ADDRESS")
    pick("WORK_MAX_DISTANCE_KM")

    times = given.get("SCHEDULE_TIMES")
    if times is not None:
        out["SCHEDULE_CRON_TIMES"] = times_to_cron(times)
    else:
        default = cron_to_times(defaults.get("SCHEDULE_CRON_TIMES", "")) or "09:00,18:00"
        if not interactive:
            out["SCHEDULE_CRON_TIMES"] = times_to_cron(default)
        else:
            while True:
                raw = prompt("Run times, 24h HH:MM, comma-separated", default=default)
                try:
                    out["SCHEDULE_CRON_TIMES"] = times_to_cron(raw)
                    break
                except ValueError as e:
                    typer.echo(f"  {e}")
    return out


def write_env(env_path: Path, template_text: str, values: dict[str, str]) -> None:
    env_path.write_text(
        set_env_values(template_text, values), encoding="utf-8", newline=""
    )


def update_env(env_path: Path, values: dict[str, str]) -> None:
    text = env_path.read_text(encoding="utf-8")
    write_env(env_path, text, values)


def create_databases(env_path: Path, values: dict[str, str]) -> dict[str, str]:
    """Create one Notion DB per enabled module and write its id into .env."""
    from .notion_setup import create_database

    def split(raw: str) -> list[str]:
        return [n.strip() for n in raw.split(",") if n.strip()]

    rent_nb = split(values.get("RENT_NEIGHBORHOODS", ""))
    buy_nb = split(values.get("BUY_NEIGHBORHOODS", "")) or rent_nb
    current = parse_env(env_path.read_text(encoding="utf-8"))
    ids: dict[str, str] = {}
    for kind in values["MODULES_ENABLED"].split(","):
        try:
            out = create_database(
                kind,
                token=values["NOTION_TOKEN"],
                parent_page_id=values["NOTION_PARENT_PAGE_ID"],
                neighborhoods=buy_nb if kind == "buy" else rent_nb,
                city=current.get(f"{kind.upper()}_CITY") or None,
            )
        except Exception as e:  # report and keep going; the token is never printed
            typer.echo(f"  {kind}: could not create the database ({type(e).__name__}: {e})")
            continue
        ids[f"NOTION_{kind.upper()}_DB_ID"] = out["id"]
        typer.echo(f"  {kind}: created {out.get('url') or out['id']}")
    if ids:
        update_env(env_path, ids)
    return ids


def run_init(
    given: dict[str, str | None],
    *,
    force: bool = False,
    interactive: bool = True,
    create_dbs: bool | None = None,
    root: Path = ROOT,
    prompt: Callable[..., str] = typer.prompt,
    confirm: Callable[..., bool] = typer.confirm,
) -> Path:
    """Write `<root>/.env` and, on request, create the Notion databases.

    `create_dbs=None` asks (interactive) or skips (non-interactive).
    """
    env_path = root / ".env"
    example_path = root / ".env.example"
    if env_path.exists() and not force:
        raise FileExistsError(f"{env_path} already exists; pass --force to overwrite it")
    if not example_path.exists():
        raise FileNotFoundError(f"{example_path} is missing; it is the template for .env")
    example_text = example_path.read_text(encoding="utf-8")
    values = collect_values(
        given, parse_env(example_text), interactive=interactive, prompt=prompt
    )
    write_env(env_path, example_text, values)
    typer.echo(f"Wrote {env_path}")
    if not values["NOTION_TOKEN"] or not values["NOTION_PARENT_PAGE_ID"]:
        typer.echo(
            "NOTION_TOKEN / NOTION_PARENT_PAGE_ID are empty; fill them in, then "
            "create the databases with scripts/setup_notion.py."
        )
        return env_path
    if create_dbs is None:
        create_dbs = interactive and confirm("Create the Notion databases now?", default=True)
    if create_dbs:
        create_databases(env_path, values)
    return env_path
