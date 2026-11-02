"""`rental-finder doctor` checks, with the Notion call and PATH stubbed."""

from __future__ import annotations

from rental_finder import doctor
from rental_finder.config import Settings


def _cfg(**kw) -> Settings:
    base = dict(
        notion_token="ntn_abc",
        notion_parent_page_id="page",
        notion_rent_db_id="rent-db",
        notion_buy_db_id="",
        modules_enabled="rent",
        condo_search_enabled=True,
        condo_search_provider="claude",
        condo_agent_cmd="claude",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def _by_name(checks):
    return {name: (status, detail) for name, status, detail in checks}


def _which(found: set[str]):
    return lambda cmd: f"/bin/{cmd}" if cmd in found else None


def test_all_green(tmp_path):
    env = tmp_path / ".env"
    env.write_text("x=1\n")
    checks = doctor.run_checks(
        _cfg(), env_path=env, which=_which({"uv", "claude"}), ping=lambda t: "bot"
    )
    assert not doctor.has_failures(checks)
    got = _by_name(checks)
    assert got["Notion reachable"] == ("OK", "authenticated as bot")
    assert got["NOTION_RENT_DB_ID"][0] == "OK"
    assert "NOTION_BUY_DB_ID" not in got  # buy not enabled
    assert got["claude on PATH"][0] == "OK"
    # Never echo the token.
    assert all("ntn_abc" not in d for _, _, d in checks)


def test_missing_env_and_bad_token(tmp_path):
    called = []
    checks = doctor.run_checks(
        _cfg(notion_token="bogus", notion_parent_page_id=""),
        env_path=tmp_path / ".env",
        which=_which(set()),
        ping=lambda t: called.append(t) or "bot",
    )
    got = _by_name(checks)
    assert got[".env exists"][0] == "FAIL"
    assert got["NOTION_TOKEN"][0] == "FAIL"
    assert got["NOTION_PARENT_PAGE_ID"][0] == "FAIL"
    assert got["Notion reachable"][0] == "SKIP"
    assert got["uv on PATH"][0] == "FAIL"
    assert called == []
    assert doctor.has_failures(checks)


def test_buy_db_required_when_buy_enabled_and_notion_down(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")

    def boom(_t):
        raise RuntimeError("unauthorized")

    checks = doctor.run_checks(
        _cfg(modules_enabled="rent,buy"),
        env_path=env,
        which=_which({"uv", "claude"}),
        ping=boom,
    )
    got = _by_name(checks)
    assert got["NOTION_BUY_DB_ID"][0] == "FAIL"
    assert got["Notion reachable"] == ("FAIL", "RuntimeError: unauthorized")


def test_claude_skipped_unless_claude_tier(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    for kw in ({"condo_search_provider": "serper"}, {"condo_search_enabled": False}):
        checks = doctor.run_checks(
            _cfg(**kw), env_path=env, which=_which({"uv"}), ping=lambda t: "bot"
        )
        assert _by_name(checks)["claude on PATH"][0] == "SKIP"
        assert not doctor.has_failures(checks)


def test_notion_call_is_the_monkeypatched_one(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(doctor, "_notion_ping", lambda token: "patched")
    checks = doctor.run_checks(_cfg(), env_path=env, which=_which({"uv", "claude"}))
    assert _by_name(checks)["Notion reachable"] == ("OK", "authenticated as patched")
