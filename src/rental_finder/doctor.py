"""`rental-finder doctor`: is this install ready to run a cycle?

Every check returns (name, status, detail) with status OK / FAIL / SKIP, so the
whole list is testable without a network: the Notion call goes through
`_notion_ping`, which tests monkeypatch.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .config import ROOT, Settings

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"
Check = tuple[str, str, str]

TOKEN_PREFIXES = ("ntn_", "secret_")
KNOWN_MODULES = ("rent", "buy")


def _notion_ping(token: str) -> str:
    """Who the token authenticates as. Raises on any failure."""
    from notion_client import Client

    me = Client(auth=token).users.me()
    return me.get("name") or me.get("id") or "bot"


def run_checks(
    cfg: Settings | None = None,
    *,
    env_path: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    ping: Callable[[str], str] | None = None,
) -> list[Check]:
    """Run every check. `cfg` defaults to a fresh Settings() read from .env now."""
    env_path = env_path or (ROOT / ".env")
    ping = ping or _notion_ping
    if cfg is None:
        cfg = Settings()
    out: list[Check] = []

    out.append(
        (".env exists", OK, str(env_path))
        if env_path.exists()
        else (".env exists", FAIL, f"{env_path} missing; run `rental-finder init`")
    )

    token = cfg.notion_token or ""
    token_ok = token.startswith(TOKEN_PREFIXES)
    if not token:
        out.append(("NOTION_TOKEN", FAIL, "not set"))
    elif not token_ok:
        out.append(("NOTION_TOKEN", FAIL, "does not start with ntn_ or secret_"))
    else:
        out.append(("NOTION_TOKEN", OK, f"{token[: token.index('_') + 1]}..."))

    out.append(
        ("NOTION_PARENT_PAGE_ID", OK, cfg.notion_parent_page_id)
        if cfg.notion_parent_page_id
        else ("NOTION_PARENT_PAGE_ID", FAIL, "not set")
    )

    modules = cfg.modules_list
    if not modules:
        out.append(("MODULES_ENABLED", FAIL, "no module enabled"))
    for mod in modules:
        key = f"NOTION_{mod.upper()}_DB_ID"
        if mod not in KNOWN_MODULES:
            out.append((f"module {mod}", FAIL, f"unknown module; use {', '.join(KNOWN_MODULES)}"))
            continue
        db_id = getattr(cfg, f"notion_{mod}_db_id", "")
        out.append(
            (key, OK, db_id)
            if db_id
            else (key, FAIL, "not set; run `rental-finder init` or scripts/setup_notion.py")
        )

    if not token_ok:
        out.append(("Notion reachable", SKIP, "no valid token"))
    else:
        try:
            out.append(("Notion reachable", OK, f"authenticated as {ping(token)}"))
        except Exception as e:  # any failure is the answer; never echo the token
            out.append(("Notion reachable", FAIL, f"{type(e).__name__}: {e}"))

    uv = which("uv")
    out.append(("uv on PATH", OK, uv) if uv else ("uv on PATH", FAIL, "not found"))

    if cfg.condo_search_enabled and cfg.condo_search_provider == "claude":
        cmd = cfg.condo_agent_cmd or "claude"
        found = which(cmd)
        out.append(
            (f"{cmd} on PATH", OK, found)
            if found
            else (
                f"{cmd} on PATH",
                FAIL,
                "not found; install Claude Code or set CONDO_SEARCH_ENABLED=false",
            )
        )
    else:
        out.append(("claude on PATH", SKIP, "condo search is not using the claude tier"))
    return out


def has_failures(checks: list[Check]) -> bool:
    return any(status == FAIL for _, status, _ in checks)
