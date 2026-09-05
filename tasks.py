"""Scheduled work of stapel-analytics — the retention sweep and the outbox drain.

Two, and the shape of the pair is the design showing through. Fan-out needs
no job: it rides the comm outbox, whose consumer the host already runs.
Reports need none: they compute on read. What is left is the work this
module cannot delegate — a table that grows without bound and holds
personal data, and a table of its own whose rows are waiting on somebody
else's API.

The second one is here because **a library that owns a retry owns the thing
that runs it.** 0.4.0 shipped the conversion outbox, the backoff and the
command, and left the sweep to each host: a deployment that wired
``get_analytics_beat_schedule()`` got durable rows and no drain, and the
failure was silent — rows sit ``pending`` forever and the dashboard looks
healthy. "Documented but never wired" is not a host's mistake to make when
the library is the one that knows the rows exist.

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

#: Names a beat schedule must reference (stable across refactors).
PURGE_TASK_NAME = "stapel_analytics.tasks.purge_analytics_events"
UPLOAD_TASK_NAME = "stapel_analytics.tasks.upload_click_conversions"

#: Rows one drain pass considers. Same number as the management command's
#: ``--limit`` default, and deliberately the same constant: the beat entry
#: and the operator's manual door must not have two different appetites.
DEFAULT_UPLOAD_LIMIT = 100


def purge_analytics_events() -> dict:
    """Drop analytics rows past ``RETENTION_DAYS``. Returns and logs counts."""
    from . import services

    removed = services.purge_events()
    if removed:
        logger.info("analytics retention purge: %s event(s) removed", removed)
    return {"removed": removed}


def upload_click_conversions(limit: int = DEFAULT_UPLOAD_LIMIT) -> dict:
    """Drain the offline click-conversion outbox. Returns counts by status.

    This IS the command's non-dry-run branch — ``analytics_upload_conversions``
    calls straight into it rather than reimplementing the loop, so the
    scheduled sweep and the operator's manual run cannot drift into two
    behaviours. (``--dry-run`` deliberately does not come through here: it
    must write nothing, and this writes.)

    An idle pass is a database query and nothing else: no rows due means no
    API call, no log line, an empty mapping. It never raises for an empty
    outbox, and it never raises for a row Google refuses — a refusal is
    recorded on the row (``conversions.deliver``), because a scheduled task
    that dies on one bad click id stops delivering the good ones behind it.
    """
    from . import conversions

    counts: dict[str, int] = {}
    for row in conversions.due(limit):
        answer = conversions.deliver(row)
        counts[answer["status"]] = counts.get(answer["status"], 0) + 1
    if counts:
        logger.info(
            "analytics conversion upload: %s",
            ", ".join(f"{status}={count}" for status, count in sorted(counts.items())),
        )
    return counts


def get_analytics_beat_schedule() -> dict:
    """Both beat entries, each on its configured cadence."""
    from celery.schedules import crontab

    from .conf import analytics_settings

    return {
        "analytics-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**dict(analytics_settings.PURGE_SCHEDULE or {})),
        },
        "analytics-upload-conversions": {
            "task": UPLOAD_TASK_NAME,
            "schedule": crontab(
                **dict(analytics_settings.CONVERSION_UPLOAD_SCHEDULE or {})
            ),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_analytics_events = shared_task(name=PURGE_TASK_NAME)(purge_analytics_events)
    upload_click_conversions = shared_task(name=UPLOAD_TASK_NAME)(
        upload_click_conversions
    )


__all__ = [
    "DEFAULT_UPLOAD_LIMIT",
    "PURGE_TASK_NAME",
    "UPLOAD_TASK_NAME",
    "get_analytics_beat_schedule",
    "purge_analytics_events",
    "upload_click_conversions",
]
