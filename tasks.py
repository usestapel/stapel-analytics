"""Scheduled work of stapel-analytics — the retention sweep.

There is only one, and that is the design showing through. The module has no
queue to drain (fan-out rides the comm outbox, whose consumer the host
already runs) and no aggregation job (reports compute on read). What it does
have is a table that grows without bound and holds personal data, so the one
scheduled thing is the one that ends rows.

Celery is OPTIONAL. The function is a plain callable a cron, a systemd timer
or any scheduler can invoke; when celery is installed it is additionally
registered as a shared task under the stable name below.

Wire it into a host's beat schedule::

    from stapel_analytics.tasks import get_analytics_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_analytics_beat_schedule(),
        ...
    }

A deployment that gives the stream a retention in ``STAPEL_EVENTSTORE``
instead can run core's ``sweep_eventstore`` and skip this entirely — which
is why ``analytics.W005`` accepts either horizon.
"""
import logging

logger = logging.getLogger(__name__)

#: Name a beat schedule must reference (stable across refactors).
PURGE_TASK_NAME = "stapel_analytics.tasks.purge_analytics_events"


def purge_analytics_events() -> dict:
    """Drop analytics rows past ``RETENTION_DAYS``. Returns and logs counts."""
    from . import services

    removed = services.purge_events()
    if removed:
        logger.info("analytics retention purge: %s event(s) removed", removed)
    return {"removed": removed}


def get_analytics_beat_schedule() -> dict:
    """Beat entry for the retention purge, on the configured cadence."""
    from celery.schedules import crontab

    from .conf import analytics_settings

    return {
        "analytics-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**dict(analytics_settings.PURGE_SCHEDULE or {})),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_analytics_events = shared_task(name=PURGE_TASK_NAME)(purge_analytics_events)


__all__ = [
    "PURGE_TASK_NAME",
    "get_analytics_beat_schedule",
    "purge_analytics_events",
]
