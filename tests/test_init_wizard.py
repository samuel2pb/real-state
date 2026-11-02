"""The .env helpers behind `rental-finder init` and the wizard end to end."""

from __future__ import annotations

import pytest

from rental_finder import init_wizard as w

EXAMPLE = """# ----- Notion -----
# Paste the secret below
NOTION_TOKEN=
NOTION_PARENT_PAGE_ID=

# ----- Search: RENT module -----
# Neighborhoods in preference order
RENT_NEIGHBORHOODS=Moema,Pinheiros
RENT_PRICE_MIN=4500
RENT_PRICE_MAX=6300
RENT_BEDROOMS_MIN=2
RENT_BEDROOMS_MAX=3
RENT_CITY=São Paulo
MODULES_ENABLED=rent
# RENT_PRICE_MAX=999 commented out stays commented
BUY_NEIGHBORHOODS=
BUY_PRICE_MIN=500000
BUY_PRICE_MAX=1200000
WORK_ADDRESS=Av. Paulista, 1000 - Bela Vista, São Paulo - SP
WORK_MAX_DISTANCE_KM=5.5
SCHEDULE_CRON_TIMES=0 9 * * *,0 18 * * *
NOTION_RENT_DB_ID=
NOTION_BUY_DB_ID=
"""


def test_replace_keeps_comments_and_order():
    out = w.set_env_values(EXAMPLE, {"RENT_PRICE_MAX": "8000", "NOTION_TOKEN": "ntn_x"})
    before = EXAMPLE.splitlines()
    after = out.splitlines()
    assert len(before) == len(after)
    for b, a in zip(before, after):
        if b.startswith("RENT_PRICE_MAX="):
            assert a == "RENT_PRICE_MAX=8000"
        elif b.startswith("NOTION_TOKEN="):
            assert a == "NOTION_TOKEN=ntn_x"
        else:
            assert a == b
    assert "# RENT_PRICE_MAX=999 commented out stays commented" in out


def test_unknown_keys_are_appended():
    out = w.set_env_values(EXAMPLE, {"NEW_KEY": "1", "RENT_PRICE_MIN": "1"})
    assert out.rstrip("\n").splitlines()[-1] == "NEW_KEY=1"
    assert out.count("NEW_KEY=") == 1
    assert "RENT_PRICE_MIN=1\n" in out


def test_values_cannot_inject_lines():
    out = w.set_env_values("A=1\n", {"A": "x\nB=2"})
    assert out == "A=x B=2\n"


def test_crlf_is_preserved():
    out = w.set_env_values("# c\r\nA=1\r\n", {"A": "2"})
    assert out == "# c\r\nA=2\r\n"


def test_parse_env_skips_comments():
    env = w.parse_env(EXAMPLE)
    assert env["RENT_PRICE_MAX"] == "6300"
    assert env["WORK_ADDRESS"].startswith("Av. Paulista, 1000")
    assert env["NOTION_TOKEN"] == ""


@pytest.mark.parametrize(
    "times, cron",
    [
        ("09:00,18:00", "0 9 * * *,0 18 * * *"),
        ("7:30", "30 7 * * *"),
        (" 23:59 , 00:00 ", "59 23 * * *,0 0 * * *"),
    ],
)
def test_times_to_cron(times, cron):
    assert w.times_to_cron(times) == cron
    assert w.times_to_cron(w.cron_to_times(cron)) == cron


@pytest.mark.parametrize("bad", ["", "9", "24:00", "12:60", "9am", "09:00,x"])
def test_times_to_cron_rejects(bad):
    with pytest.raises(ValueError):
        w.times_to_cron(bad)


def test_cron_to_times_gives_up_on_non_daily():
    assert w.cron_to_times("0 9 * * 1") == ""
    assert w.cron_to_times("*/5 * * * *") == ""


def test_normalize_modules():
    assert w.normalize_modules("rent") == "rent"
    assert w.normalize_modules("Buy, rent") == "rent,buy"
    assert w.normalize_modules("both") == "rent,buy"
    with pytest.raises(ValueError):
        w.normalize_modules("sell")


def test_normalize_page_id_from_url():
    url = "https://www.notion.so/me/Apartments-0123456789abcdef0123456789ABCDEF?pvs=4"
    assert w.normalize_page_id(url) == "01234567-89ab-cdef-0123-456789abcdef"
    assert w.normalize_page_id("0123456789abcdef0123456789abcdef") == (
        "01234567-89ab-cdef-0123-456789abcdef"
    )


def _given(**kw):
    keys = (
        "NOTION_TOKEN NOTION_PARENT_PAGE_ID MODULES_ENABLED RENT_NEIGHBORHOODS "
        "RENT_PRICE_MIN RENT_PRICE_MAX RENT_BEDROOMS_MIN RENT_BEDROOMS_MAX "
        "BUY_NEIGHBORHOODS BUY_PRICE_MIN BUY_PRICE_MAX WORK_ADDRESS "
        "WORK_MAX_DISTANCE_KM SCHEDULE_TIMES"
    ).split()
    return {k: kw.get(k) for k in keys}


def test_non_interactive_writes_env(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    env = w.run_init(
        _given(
            NOTION_TOKEN="ntn_secret",
            NOTION_PARENT_PAGE_ID="0123456789abcdef0123456789abcdef",
            MODULES_ENABLED="rent,buy",
            RENT_PRICE_MAX="7000",
            BUY_NEIGHBORHOODS="Itaim Bibi",
            SCHEDULE_TIMES="08:15",
        ),
        interactive=False,
        root=tmp_path,
    )
    vals = w.parse_env(env.read_text(encoding="utf-8"))
    assert vals["NOTION_TOKEN"] == "ntn_secret"
    assert vals["NOTION_PARENT_PAGE_ID"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert vals["MODULES_ENABLED"] == "rent,buy"
    assert vals["RENT_PRICE_MAX"] == "7000"
    assert vals["RENT_PRICE_MIN"] == "4500"  # example default kept
    assert vals["BUY_NEIGHBORHOODS"] == "Itaim Bibi"
    assert vals["SCHEDULE_CRON_TIMES"] == "15 8 * * *"
    assert vals["RENT_CITY"] == "São Paulo"
    assert "# Paste the secret below" in env.read_text(encoding="utf-8")


def test_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / ".env").write_text("KEEP=1\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        w.run_init(_given(), interactive=False, root=tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "KEEP=1\n"
    w.run_init(_given(), interactive=False, force=True, root=tmp_path)
    assert "KEEP=1" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_interactive_prompts_and_creates_dbs(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    token_label = "Notion integration token (ntn_... / secret_...)"
    answers = {
        token_label: "secret_abc",
        w._LABELS["NOTION_PARENT_PAGE_ID"]: "0123456789abcdef0123456789abcdef",
        w._LABELS["MODULES_ENABLED"]: "buy",
        w._LABELS["BUY_PRICE_MAX"]: "900000",
    }
    asked = []

    def prompt(text, default=None, hide_input=False):
        asked.append((text, hide_input))
        return answers.get(text, default)

    created = []

    def fake_create(kind, **kw):
        created.append((kind, kw))
        return {"id": f"{kind}-db", "url": None}

    monkeypatch.setattr("rental_finder.notion_setup.create_database", fake_create)
    env = w.run_init(
        _given(), root=tmp_path, prompt=prompt, confirm=lambda *a, **k: True
    )
    assert (token_label, True) in asked
    assert any(t == w._LABELS["BUY_PRICE_MAX"] for t, _ in asked)
    vals = w.parse_env(env.read_text(encoding="utf-8"))
    assert vals["MODULES_ENABLED"] == "buy"
    assert vals["BUY_PRICE_MAX"] == "900000"
    assert vals["NOTION_BUY_DB_ID"] == "buy-db"
    assert vals["NOTION_RENT_DB_ID"] == ""
    assert [k for k, _ in created] == ["buy"]
    # BUY_NEIGHBORHOODS blank -> falls back to the rent list
    assert created[0][1]["neighborhoods"] == ["Moema", "Pinheiros"]
    assert created[0][1]["token"] == "secret_abc"
