"""In-process timers: poll the mailbox, and keep an eye on stuck print jobs."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config
from .service import POLL_INTERVAL_KEY, AgentService

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
        id="poll",
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
    return scheduler


__all__ = [
    "build_scheduler",
    "poll_interval_min",
    "retry_and_reap",
    "RETRY_INTERVAL_MIN",
    "PRUNE_INTERVAL_HOURS",
]
