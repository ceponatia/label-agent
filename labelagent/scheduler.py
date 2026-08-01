"""In-process timers: poll the mailbox, and keep an eye on stuck print jobs."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config
from .service import POLL_INTERVAL_KEY, AgentService

POLL_JOB_ID = "poll"
RETRY_INTERVAL_MIN = 2
PRUNE_INTERVAL_HOURS = 24


def poll_interval_min(service: AgentService, config: Config) -> int:
    """Poll interval from Settings, falling back to config.toml."""
    raw = service.db.get_setting(POLL_INTERVAL_KEY, str(config.poll_interval_min))
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return max(1, config.poll_interval_min)


def retry_and_reap(service: AgentService) -> None:
    service.retry_waiting()
    service.reap_jobs()


def build_scheduler(service: AgentService, config: Config) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        service.check_now,
        "interval",
        minutes=poll_interval_min(service, config),
        id=POLL_JOB_ID,
        name="poll gmail",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        retry_and_reap,
        "interval",
        args=[service],
        minutes=RETRY_INTERVAL_MIN,
        id="retry",
        name="retry waiting prints",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        service.prune_old_files,
        "interval",
        hours=PRUNE_INTERVAL_HOURS,
        id="prune",
        name="prune old label files",
        coalesce=True,
        max_instances=1,
    )
    attach_poll_rescheduler(scheduler, service, config)
    return scheduler


def attach_poll_rescheduler(
    scheduler: BackgroundScheduler, service: AgentService, config: Config
) -> None:
    """Give the service a way to retime the poll job on this scheduler.

    The interval is baked into the job when it is created, so a new value saved
    in Settings used to sit in the table doing nothing until the next restart.
    """

    def reschedule() -> int:
        minutes = poll_interval_min(service, config)
        scheduler.reschedule_job(POLL_JOB_ID, trigger="interval", minutes=minutes)
        return minutes

    service.set_poll_rescheduler(reschedule)


__all__ = [
    "attach_poll_rescheduler",
    "build_scheduler",
    "poll_interval_min",
    "retry_and_reap",
    "POLL_JOB_ID",
    "RETRY_INTERVAL_MIN",
    "PRUNE_INTERVAL_HOURS",
]
