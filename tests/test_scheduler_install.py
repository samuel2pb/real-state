"""`rental-finder install-scheduler`, with every subprocess stubbed.

`_run` is the only process seam, so each test swaps it for a recorder and
forces the OS; nothing here touches a real scheduler.
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from rental_finder import scheduler_install as si
from rental_finder.scheduler_install import SchedulerError, Trigger

UV_POSIX = "/home/me/.local/bin/uv"
UV_WIN = r"C:\Users\me\.local\bin\uv.exe"


class Recorder:
    """Stands in for `_run`; answers reads from a canned stdout map."""

    def __init__(self, replies: dict[str, subprocess.CompletedProcess] | None = None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.replies = replies or {}

    def __call__(self, cmd, dry_run, *, input=None, check=True):
        self.calls.append((list(cmd), input))
        key = " ".join(cmd[:2])
        if key in self.replies:
            return self.replies[key]
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _ok(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, "")


# ---------------------------------------------------------------- cron parsing


def test_parse_daily():
    assert si.parse_cron("0 9 * * *") == Trigger(9, 0)
    assert si.parse_cron("30 18 * * *") == Trigger(18, 30)


def test_parse_weekly_numeric():
    assert si.parse_cron("15 7 * * 1") == Trigger(7, 15, 1)
    assert si.parse_cron("0 7 * * 7") == Trigger(7, 0, 0)  # 7 = Sunday too


def test_parse_weekly_name():
    assert si.parse_cron("0 6 * * MON") == Trigger(6, 0, 1)
    assert si.parse_cron("0 6 * * sun") == Trigger(6, 0, 0)


@pytest.mark.parametrize(
    "expr",
    [
        "*/15 * * * *",
        "0 9 1 * *",
        "0 9 * 6 *",
        "0 9 * * 1-5",
        "0 9,18 * * *",
        "0 25 * * *",
        "0 9 * *",
    ],
)
def test_parse_unsupported(expr):
    with pytest.raises(SchedulerError, match="supported shapes|hour 0-23"):
        si.parse_cron(expr)


def test_parse_crons_dedupes_and_requires_one():
    assert si.parse_crons(["0 9 * * *", "0 9 * * *"]) == [Trigger(9, 0)]
    with pytest.raises(SchedulerError):
        si.parse_crons([])


def test_triggers_from_times():
    assert si.triggers_from_times("09:00,18:30") == [Trigger(9, 0), Trigger(18, 30)]
    with pytest.raises(SchedulerError):
        si.triggers_from_times("9am")


def test_resolve_kinds_and_subcommands():
    assert si.resolve_kinds(None, ["rent", "buy"]) == ["rent", "buy"]
    assert si.resolve_kinds("both", ["rent"]) == ["rent", "buy"]
    assert si.resolve_kinds("buy", ["rent"]) == ["buy"]
    with pytest.raises(SchedulerError):
        si.resolve_kinds("sell", ["rent"])
    assert si.subcommand("run", "rent") == ["run-once"]
    assert si.subcommand("run", "buy") == ["run-buy-once"]
    assert si.subcommand("recheck-gone", "buy") == ["recheck-gone", "--kind", "buy"]


def test_missing_uv_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(si, "_run", Recorder())
    with pytest.raises(SchedulerError, match="uv is not on PATH"):
        si.install(
            kinds=["rent"], job="run", triggers=[Trigger(9, 0)],
            os_name="Linux", root="/opt/rf", which=lambda _: None,
        )


# ---------------------------------------------------------------- Windows


def test_windows_install_commands(monkeypatch):
    root = r"C:\Users\me\rental finder"
    rec = Recorder()
    monkeypatch.setattr(si, "_run", rec)
    done = si.install(
        kinds=["rent"], job="run",
        triggers=[Trigger(9, 0), Trigger(7, 30, 1)],
        os_name="Windows", root=root, which=lambda _: UV_WIN, dry_run=True,
    )
    cmds = [c for c, _ in rec.calls]
    assert cmds[0] == ["schtasks", "/Query", "/FO", "CSV", "/NH"]
    tr = (
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{root}\\scripts\\run-task.ps1" '
        f'-Uv "{UV_WIN}" -Log "logs\\run-rent.log" -Run "run-once"'
    )
    assert cmds[1] == [
        "schtasks", "/Create", "/F", "/TN", r"RentalFinder\run-rent-0900",
        "/SC", "DAILY", "/ST", "09:00", "/TR", tr,
    ]
    assert cmds[2] == [
        "schtasks", "/Create", "/F", "/TN", r"RentalFinder\run-rent-MON-0730",
        "/SC", "WEEKLY", "/D", "MON", "/ST", "07:30", "/TR", tr,
    ]
    assert done == [
        r"created task RentalFinder\run-rent-0900",
        r"created task RentalFinder\run-rent-MON-0730",
    ]


def test_windows_recheck_gone_command():
    tr = si.windows_tr(UV_WIN, r"C:\rf", "recheck-gone", "buy")
    assert '-Run "recheck-gone --kind buy"' in tr
    assert r'-Log "logs\recheck-gone-buy.log"' in tr


def test_windows_hidden_command():
    root = r"C:\Users\me\rental finder"
    tr = si.windows_tr(UV_WIN, root, "run", "rent", window=False)
    assert tr == (
        f'conhost.exe --headless cmd /c "cd /d "{root}" && "{UV_WIN}" run rental-finder run-once '
        f'>> "logs\\run-rent.log" 2>&1"'
    )


def test_windows_commands_fit_this_checkout():
    # The longest job/kind on a typical install path must stay under schtasks' limit.
    root = r"C:\Users\someone.long\rental-finder"
    for window in (True, False):
        assert len(si.windows_tr(UV_WIN, root, "recheck-gone", "rent", window=window)) <= 261


@pytest.mark.parametrize("window", [True, False])
def test_windows_tr_too_long(window):
    with pytest.raises(SchedulerError, match="261"):
        si.windows_tr(UV_WIN, "C:\\" + "x" * 250, "run", "rent", window=window)


def test_windows_install_replaces_stale_and_uninstall(monkeypatch):
    query = (
        '"\\RentalFinder\\run-rent-0900","N/A","Ready"\r\n'
        '"\\RentalFinder\\run-rent-2100","N/A","Ready"\r\n'
        '"\\RentalFinder\\run-buy-0900","N/A","Ready"\r\n'
        '"\\Microsoft\\Windows\\Other","N/A","Ready"\r\n'
    )
    rec = Recorder({"schtasks /Query": _ok(query)})
    monkeypatch.setattr(si, "_run", rec)
    si.install(
        kinds=["rent"], job="run", triggers=[Trigger(9, 0)], uninstall=True,
        os_name="Windows", root=r"C:\rf", which=lambda _: UV_WIN,
    )
    deletes = [c for c, _ in rec.calls if c[1] == "/Delete"]
    assert deletes == [
        ["schtasks", "/Delete", "/F", "/TN", r"RentalFinder\run-rent-0900"],
        ["schtasks", "/Delete", "/F", "/TN", r"RentalFinder\run-rent-2100"],
    ]
    assert not [c for c, _ in rec.calls if c[1] == "/Create"]


# ---------------------------------------------------------------- macOS


def test_macos_plist_round_trip_and_commands(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(si, "_run", rec)
    root = tmp_path / "rf"
    si.install(
        kinds=["buy"], job="run",
        triggers=[Trigger(9, 0), Trigger(6, 15, 0)],
        os_name="Darwin", root=root.as_posix(), which=lambda _: UV_POSIX,
        home=tmp_path, uid=501, path_env="/usr/bin:/bin",
    )
    plist = tmp_path / "Library" / "LaunchAgents" / "com.rental-finder.run-buy.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "com.rental-finder.run-buy"
    assert data["ProgramArguments"] == [
        UV_POSIX, "run", "--directory", root.as_posix(), "rental-finder", "run-buy-once",
    ]
    assert data["WorkingDirectory"] == root.as_posix()
    assert data["StartCalendarInterval"] == [
        {"Hour": 9, "Minute": 0},
        {"Hour": 6, "Minute": 15, "Weekday": 0},
    ]
    log = f"{root.as_posix()}/logs/run-buy.log"
    assert data["StandardOutPath"] == data["StandardErrorPath"] == log
    assert data["EnvironmentVariables"]["PATH"] == "/home/me/.local/bin:/usr/bin:/bin"
    cmds = [c for c, _ in rec.calls]
    assert cmds == [
        ["launchctl", "bootout", "gui/501", str(plist)],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
    ]

    rec.calls.clear()
    si.install(
        kinds=["buy"], job="run", triggers=[Trigger(9, 0)], uninstall=True,
        os_name="Darwin", root=root.as_posix(), which=lambda _: UV_POSIX,
        home=tmp_path, uid=501,
    )
    assert not plist.exists()
    assert [c for c, _ in rec.calls] == [["launchctl", "bootout", "gui/501", str(plist)]]


def test_macos_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(si, "_run", Recorder())
    si.install(
        kinds=["rent"], job="run", triggers=[Trigger(9, 0)], dry_run=True,
        os_name="Darwin", root=(tmp_path / "rf").as_posix(), which=lambda _: UV_POSIX,
        home=tmp_path, uid=501, path_env="/usr/bin",
    )
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / "rf").exists()
    assert "<key>StartCalendarInterval</key>" in capsys.readouterr().out


def test_dry_run_runner_executes_nothing(capsys):
    out = si._run(["definitely-not-a-real-binary", "--x"], True, input="a\nb")
    assert out.returncode == 0 and out.stdout == ""
    printed = capsys.readouterr().out
    assert "definitely-not-a-real-binary" in printed and "  a" in printed


# ---------------------------------------------------------------- Linux


def test_cron_lines():
    lines = si.cron_lines(
        "run", "rent", [Trigger(9, 0), Trigger(7, 30, 1)], UV_POSIX, "/home/me/rf dir", "/usr/bin"
    )
    tail = (
        f"PATH=/usr/bin {UV_POSIX} run --directory '/home/me/rf dir' "
        "rental-finder run-once >> '/home/me/rf dir/logs/run-rent.log' 2>&1 "
        "# rental-finder:run-rent"
    )
    assert lines == [f"0 9 * * * {tail}", f"30 7 * * 1 {tail}"]


def test_replace_tagged_is_idempotent_and_keeps_others():
    current = (
        "MAILTO=me\n"
        "0 1 * * * backup.sh\n"
        "0 8 * * * old # rental-finder:run-rent\n"
        "0 8 * * * keep # rental-finder:run-buy\n"
    )
    new = ["0 9 * * * new # rental-finder:run-rent"]
    once = si.replace_tagged(current, "run", "rent", new)
    twice = si.replace_tagged(once, "run", "rent", new)
    assert once == twice
    assert once == (
        "MAILTO=me\n0 1 * * * backup.sh\n0 8 * * * keep # rental-finder:run-buy\n"
        "0 9 * * * new # rental-finder:run-rent\n"
    )
    removed = si.replace_tagged(once, "run", "rent", [])
    assert removed == si.replace_tagged(removed, "run", "rent", [])
    assert "run-rent" not in removed and "backup.sh" in removed
    assert si.replace_tagged("", "run", "rent", []) == ""


def test_linux_install_twice_same_crontab(monkeypatch, tmp_path):
    state: dict[str, str | None] = {"crontab": None}

    def fake(cmd, dry_run, *, input=None, check=True):
        if cmd == ["crontab", "-l"]:
            if state["crontab"] is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no crontab for me")
            return subprocess.CompletedProcess(cmd, 0, state["crontab"], "")
        assert cmd == ["crontab", "-"]
        state["crontab"] = input
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(si, "_run", fake)
    kw = dict(
        kinds=["rent", "buy"], job="run", triggers=[Trigger(9, 0), Trigger(18, 0)],
        os_name="Linux", root=(tmp_path / "rf").as_posix(), which=lambda _: UV_POSIX,
        path_env="/usr/bin",
    )
    si.install(**kw)
    first = state["crontab"]
    si.install(**kw)
    assert state["crontab"] == first
    assert first.count("# rental-finder:run-rent") == 2
    assert first.count("# rental-finder:run-buy") == 2
    assert "rental-finder run-buy-once" in first

    si.install(**{**kw, "uninstall": True})
    assert state["crontab"] == ""
