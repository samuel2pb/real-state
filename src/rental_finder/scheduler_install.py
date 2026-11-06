"""`rental-finder install-scheduler`: register the cycles with the OS scheduler.

One code path per OS, all driven by the same triggers:

- Windows: one Task Scheduler task per time, under the `RentalFinder\\` folder.
- macOS:   one launchd agent per job/kind, one StartCalendarInterval per time.
- Linux:   crontab lines tagged `# rental-finder:<job>-<kind>`.

Every subprocess goes through `_run`, which prints instead of executing under
`--dry-run` and which tests monkeypatch. The OS is a parameter, so each branch
is testable from any machine.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from .config import ROOT

KINDS = ("rent", "buy")
JOBS = ("run", "recheck-gone")
TASK_FOLDER = "RentalFinder"
CRON_TAG = "# rental-finder:"
# schtasks refuses a /TR value longer than this.
SCHTASKS_TR_MAX = 261

_DAY_NAMES = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


class SchedulerError(ValueError):
    """Bad input or an environment the installer cannot work with."""


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    hour: int
    minute: int
    weekday: int | None = None  # 0 = Sunday .. 6 = Saturday; None = daily

    @property
    def hhmm(self) -> str:
        return f"{self.hour:02d}{self.minute:02d}"

    @property
    def day_name(self) -> str | None:
        return None if self.weekday is None else _DAY_NAMES[self.weekday]

    def cron(self) -> str:
        dow = "*" if self.weekday is None else str(self.weekday)
        return f"{self.minute} {self.hour} * * {dow}"


_SUPPORTED = (
    "supported shapes: daily 'M H * * *' (e.g. '0 9 * * *') and weekly "
    "'M H * * D' with D as 0-6 or SUN..SAT (e.g. '30 7 * * MON')"
)


def parse_cron(expr: str) -> Trigger:
    """'0 9 * * *' -> daily 09:00; '30 7 * * MON' -> Mondays 07:30."""
    parts = expr.split()
    if len(parts) != 5:
        raise SchedulerError(f"unsupported cron {expr!r}: need 5 fields; {_SUPPORTED}")
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*":
        raise SchedulerError(f"unsupported cron {expr!r}; {_SUPPORTED}")
    if not (minute.isdigit() and hour.isdigit()):
        raise SchedulerError(
            f"unsupported cron {expr!r}: minute and hour must be single numbers; {_SUPPORTED}"
        )
    m, h = int(minute), int(hour)
    if m > 59 or h > 23:
        raise SchedulerError(f"bad cron {expr!r}: hour 0-23, minute 0-59")
    if dow == "*":
        return Trigger(h, m)
    if dow.isdigit() and int(dow) <= 7:
        return Trigger(h, m, int(dow) % 7)  # cron allows 7 for Sunday
    if dow.upper() in _DAY_NAMES:
        return Trigger(h, m, _DAY_NAMES.index(dow.upper()))
    raise SchedulerError(f"unsupported cron {expr!r}: day-of-week {dow!r}; {_SUPPORTED}")


def parse_crons(exprs: list[str]) -> list[Trigger]:
    out: list[Trigger] = []
    for e in exprs:
        t = parse_cron(e)
        if t not in out:
            out.append(t)
    if not out:
        raise SchedulerError("no schedule times; set SCHEDULE_CRON_TIMES or pass --times")
    return out


def triggers_from_times(times: str) -> list[Trigger]:
    """'09:00,18:00' -> two daily triggers (same rules as `rental-finder init`)."""
    from .init_wizard import times_to_cron

    try:
        return parse_crons(times_to_cron(times).split(","))
    except ValueError as e:
        raise SchedulerError(str(e)) from None


# --------------------------------------------------------------------------
# What gets run
# --------------------------------------------------------------------------


def resolve_kinds(kind: str | None, modules: list[str]) -> list[str]:
    if kind is None:
        kinds = [m for m in modules if m in KINDS]
        if not kinds:
            raise SchedulerError(
                "MODULES_ENABLED has neither rent nor buy; pass --kind rent|buy|both"
            )
        return kinds
    if kind == "both":
        return list(KINDS)
    if kind not in KINDS:
        raise SchedulerError(f"--kind must be rent, buy or both (got {kind!r})")
    return [kind]


def subcommand(job: str, kind: str) -> list[str]:
    if job == "run":
        return ["run-once"] if kind == "rent" else ["run-buy-once"]
    if job == "recheck-gone":
        return ["recheck-gone", "--kind", kind]
    raise SchedulerError(f"--job must be one of {', '.join(JOBS)} (got {job!r})")


def job_name(job: str, kind: str) -> str:
    return f"{job}-{kind}"


def log_file(root: Path | str, job: str, kind: str, os_name: str) -> str:
    base = PureWindowsPath(root) if os_name == "Windows" else PurePosixPath(root)
    return str(base / "logs" / f"{job_name(job, kind)}.log")


def find_uv(which: Callable[[str], str | None] = shutil.which) -> str:
    uv = which("uv")
    if not uv:
        raise SchedulerError(
            "uv is not on PATH; install it (https://docs.astral.sh/uv/) and retry"
        )
    return uv


# --------------------------------------------------------------------------
# Subprocess seam
# --------------------------------------------------------------------------


def _fmt(cmd: list[str]) -> str:
    if platform.system() == "Windows":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _run(
    cmd: list[str],
    dry_run: bool,
    *,
    input: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """The only place a process is started. Dry run prints and returns empty."""
    if dry_run:
        print(f"$ {_fmt(cmd)}")
        if input is not None:
            print("  <<stdin")
            for line in input.splitlines():
                print(f"  {line}")
            print("  stdin")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(
        cmd,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


# --------------------------------------------------------------------------
# Windows: schtasks
# --------------------------------------------------------------------------


def windows_task_name(job: str, kind: str, t: Trigger) -> str:
    when = t.hhmm if t.weekday is None else f"{t.day_name}-{t.hhmm}"
    return f"{TASK_FOLDER}\\{job_name(job, kind)}-{when}"


def windows_tr(uv: str, root: str, job: str, kind: str, *, window: bool = True) -> str:
    """The /TR value for one job/kind.

    With a window (default): `scripts\\run-task.ps1` runs the cycle in a console
    that shows the output live and tees it into the log. Closing that window
    stops the run.

    Without (`window=False`): cmd /c, cd into the repo, run, append to the log,
    under `conhost --headless` so no window opens at all. A plain cmd would open
    an empty one (all output goes to the log), and closing it kills the run with
    0xC000013A.

    `cd /d` + a relative log path instead of `--directory <root>` + an absolute
    log: the repo path would otherwise appear twice and blow schtasks' 261-char
    limit on a deep checkout. The outer quotes are cmd's: with more than two
    quotes after /c it strips the first and last, leaving the inner ones intact.
    """
    sub = " ".join(subcommand(job, kind))
    log = f"logs\\{job_name(job, kind)}.log"
    if window:
        script = PureWindowsPath(root) / "scripts" / "run-task.ps1"
        tr = (
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" '
            f'-Uv "{uv}" -Log "{log}" -Run "{sub}"'
        )
    else:
        inner = f'cd /d "{root}" && "{uv}" run rental-finder {sub} >> "{log}" 2>&1'
        tr = f'conhost.exe --headless cmd /c "{inner}"'
    if len(tr) > SCHTASKS_TR_MAX:
        raise SchedulerError(
            f"the task command is {len(tr)} chars, over schtasks' {SCHTASKS_TR_MAX} "
            f"limit; move the repo to a shorter path. Command: {tr}"
        )
    return tr


def windows_create_cmd(name: str, t: Trigger, tr: str) -> list[str]:
    cmd = ["schtasks", "/Create", "/F", "/TN", name]
    if t.weekday is None:
        cmd += ["/SC", "DAILY"]
    else:
        cmd += ["/SC", "WEEKLY", "/D", t.day_name]
    return cmd + ["/ST", f"{t.hour:02d}:{t.minute:02d}", "/TR", tr]


def windows_existing(query_stdout: str, job: str, kind: str) -> list[str]:
    """Task names (without the leading backslash) for this job/kind.

    CSV rather than /FO LIST: the LIST labels are localized ("Nome da tarefa:"
    on a pt-BR Windows), the first CSV column is the name in every locale.
    """
    prefix = f"\\{TASK_FOLDER}\\{job_name(job, kind)}-".lower()
    out: list[str] = []
    for row in csv.reader(io.StringIO(query_stdout)):
        if not row:
            continue
        name = row[0].strip()
        if name.lower().startswith(prefix) and name[1:] not in out:
            out.append(name[1:])
    return out


def _windows(job, kind, triggers, uv, root, *, uninstall, dry_run, window) -> list[str]:
    done: list[str] = []
    query = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"], dry_run, check=False)
    for name in windows_existing(query.stdout or "", job, kind):
        _run(["schtasks", "/Delete", "/F", "/TN", name], dry_run)
        done.append(f"removed task {name}")
    if uninstall:
        return done
    tr = windows_tr(uv, root, job, kind, window=window)
    for t in triggers:
        name = windows_task_name(job, kind, t)
        _run(windows_create_cmd(name, t, tr), dry_run)
        done.append(f"created task {name}")
    return done


# --------------------------------------------------------------------------
# macOS: launchd
# --------------------------------------------------------------------------


def plist_label(job: str, kind: str) -> str:
    return f"com.rental-finder.{job_name(job, kind)}"


def build_plist(
    job: str, kind: str, triggers: list[Trigger], uv: str, root: str, path_env: str
) -> bytes:
    log = log_file(root, job, kind, "Darwin")
    intervals = []
    for t in triggers:
        d = {"Hour": t.hour, "Minute": t.minute}
        if t.weekday is not None:
            d["Weekday"] = t.weekday
        intervals.append(d)
    data = {
        "Label": plist_label(job, kind),
        "ProgramArguments": [uv, "run", "--directory", root, "rental-finder", *subcommand(job, kind)],
        "WorkingDirectory": root,
        "StartCalendarInterval": intervals,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # launchd's own PATH is /usr/bin:/bin:/usr/sbin:/sbin; the buy cycle
        # shells out to `claude`, which lives somewhere the user's shell knows.
        "EnvironmentVariables": {"PATH": path_env},
    }
    return plistlib.dumps(data)


def _macos(job, kind, triggers, uv, root, *, uninstall, dry_run, home, uid, path_env) -> list[str]:
    plist = Path(home) / "Library" / "LaunchAgents" / f"{plist_label(job, kind)}.plist"
    domain = f"gui/{uid}"
    _run(["launchctl", "bootout", domain, str(plist)], dry_run, check=False)
    if uninstall:
        if dry_run:
            print(f"# would delete {plist}")
        elif plist.exists():
            plist.unlink()
        return [f"removed {plist}"]
    body = build_plist(job, kind, triggers, uv, root, path_env)
    if dry_run:
        print(f"# would write {plist}:")
        print(body.decode("utf-8"))
    else:
        plist.parent.mkdir(parents=True, exist_ok=True)
        (Path(root) / "logs").mkdir(parents=True, exist_ok=True)
        plist.write_bytes(body)
    _run(["launchctl", "bootstrap", domain, str(plist)], dry_run)
    return [f"installed {plist}"]


# --------------------------------------------------------------------------
# Linux: crontab
# --------------------------------------------------------------------------


def cron_lines(
    job: str, kind: str, triggers: list[Trigger], uv: str, root: str, path_env: str
) -> list[str]:
    log = log_file(root, job, kind, "Linux")
    run = shlex.join([uv, "run", "--directory", root, "rental-finder", *subcommand(job, kind)])
    # cron's PATH is /usr/bin:/bin; see build_plist for why it is carried over.
    env = f"PATH={shlex.quote(path_env)} " if path_env else ""
    tag = f"{CRON_TAG}{job_name(job, kind)}"
    return [f"{t.cron()} {env}{run} >> {shlex.quote(log)} 2>&1 {tag}" for t in triggers]


def replace_tagged(current: str, job: str, kind: str, new_lines: list[str]) -> str:
    """`current` crontab with this job/kind's lines swapped for `new_lines`."""
    tag = f"{CRON_TAG}{job_name(job, kind)}"
    kept = [ln for ln in current.splitlines() if not ln.rstrip().endswith(tag)]
    while kept and not kept[-1].strip():
        kept.pop()
    lines = kept + new_lines
    return "\n".join(lines) + "\n" if lines else ""


def _linux(job, kind, triggers, uv, root, *, uninstall, dry_run, path_env) -> list[str]:
    cur = _run(["crontab", "-l"], dry_run, check=False)
    # Exit 1 with "no crontab for <user>" just means empty.
    current = cur.stdout if cur.returncode == 0 else ""
    new = [] if uninstall else cron_lines(job, kind, triggers, uv, root, path_env)
    text = replace_tagged(current or "", job, kind, new)
    if not dry_run and not uninstall:
        (Path(root) / "logs").mkdir(parents=True, exist_ok=True)
    _run(["crontab", "-"], dry_run, input=text)
    tag = f"{CRON_TAG}{job_name(job, kind)}"
    return [f"removed crontab lines {tag}"] if uninstall else [
        f"crontab: {len(new)} line(s) tagged {tag}"
    ]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def install(
    *,
    kinds: list[str],
    job: str,
    triggers: list[Trigger],
    uninstall: bool = False,
    dry_run: bool = False,
    window: bool = True,
    os_name: str | None = None,
    root: Path | str = ROOT,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | str | None = None,
    uid: int | None = None,
    path_env: str | None = None,
) -> list[str]:
    """Install (or remove) one OS schedule entry set per kind. Returns a summary.

    `window` only matters on Windows: show each run in a console (see `windows_tr`).
    """
    os_name = os_name or platform.system()
    if job not in JOBS:
        raise SchedulerError(f"--job must be one of {', '.join(JOBS)} (got {job!r})")
    root = str(root)
    uv = "" if uninstall else find_uv(which)
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    if uv and os_name != "Windows":
        uv_dir = str(PurePosixPath(uv).parent)
        if uv_dir not in path_env.split(":"):
            path_env = f"{uv_dir}:{path_env}" if path_env else uv_dir
    if os_name == "Windows" and not dry_run and not uninstall:
        (Path(root) / "logs").mkdir(parents=True, exist_ok=True)

    done: list[str] = []
    for kind in kinds:
        if os_name == "Windows":
            done += _windows(
                job, kind, triggers, uv, root,
                uninstall=uninstall, dry_run=dry_run, window=window,
            )
        elif os_name == "Darwin":
            done += _macos(
                job, kind, triggers, uv, root,
                uninstall=uninstall, dry_run=dry_run,
                home=home or Path.home(),
                uid=uid if uid is not None else os.getuid(),  # type: ignore[attr-defined]
                path_env=path_env,
            )
        elif os_name == "Linux":
            done += _linux(
                job, kind, triggers, uv, root,
                uninstall=uninstall, dry_run=dry_run, path_env=path_env,
            )
        else:
            raise SchedulerError(
                f"unsupported OS {os_name!r}; use `rental-finder schedule` in the foreground"
            )
    return done
