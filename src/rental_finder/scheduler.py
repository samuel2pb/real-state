from __future__ import annotations

import signal
import time

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .pipeline import run_rent_cycle

log = structlog.get_logger(__name__)


def _job() -> None:
    try:
        stats = run_rent_cycle()
        log.info("cycle_done", **stats)
    except Exception as e:
        log.exception("cycle_error", err=str(e))


def start() -> None:
    sched = BlockingScheduler(timezone=settings.schedule_tz)
    for cron_expr in settings.cron_list:
        parts = cron_expr.split()
        if len(parts) != 5:
            log.warning("bad_cron", cron=cron_expr); continue
        minute, hour, dom, month, dow = parts
        sched.add_job(
            _job,
            CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow,
                        timezone=settings.schedule_tz),
            name=f"rent_{cron_expr}",
        )
        log.info("scheduled", cron=cron_expr, tz=settings.schedule_tz)

    signal.signal(signal.SIGINT,  lambda *_: sched.shutdown(wait=False))
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("scheduler_started")
    sched.start()
