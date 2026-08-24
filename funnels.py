"""Funnels — conversion by step, over a window, with an optional comparison.

This is analytics-standard §3's "conversion by step, over a period, versus
another one — with no pretence of being BI", and the deliberate smallness is
the design. The
question a funnel answers is "where do people stop", and the module that
answers it well for every deployment in the fleet is worth more than the
module that answers forty questions badly for one.

**Definition** (``FunnelSpec``) comes from two places, merged: rows of the
``Funnel`` table (authored over the API) OVER the declared funnels of
``STAPEL_ANALYTICS["FUNNELS"]`` (the Studio project spec, §4). Same
merge-registry shape as everything else here, with one asymmetry: a declared
funnel is read-only over the API, because its home is the spec and an edit
that the next deploy silently reverts is worse than a refusal.

**Computation.** One pass over the stream for the step names in the period,
grouped by SUBJECT (``SUBJECT_RESOLVER`` — user hash, else anonymous id,
else session id). Per subject the steps are walked in order: the earliest
step-1 event opens the window; each later step counts only if it happened
at or after the previous step and within ``window_seconds`` of the FIRST
one. That is the classic conversion window, and stating it here matters
because the alternative (window from the previous step) gives different
numbers for the same data and neither is "wrong".

The pass is bounded by ``MAX_REPORT_EVENTS``. A report that hits the bound
comes back with ``truncated: true`` rather than becoming an unbounded scan
somebody triggers by widening a date picker.

**Why in Python and not in SQL.** The subject key is a seam, the window is
per-subject, and the store is swappable (Postgres today, ClickHouse at
scale). A GROUP BY that assumed all three would be the fastest thing in the
module and the first thing to delete. ``store.rollup`` covers the aggregate
that IS pushable; this covers the one that is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class UnknownFunnel(Exception):
    """No funnel — authored or declared — answers to this slug."""


@dataclass
class FunnelSpec:
    """A funnel, wherever it came from."""

    slug: str
    steps: list
    window_seconds: int
    title: str = ""
    description: str = ""
    #: ``"db"`` (authored over the API) or ``"settings"`` (declared in the
    #: project spec). Presented, because "why can I not edit this" is the
    #: first question a declared funnel provokes.
    source: str = "db"
    is_active: bool = True
    id: str | None = None
    owner_id: str | None = None
    workspace_id: str | None = None


@dataclass
class StepResult:
    """One step of a computed report."""

    name: str
    count: int
    #: Share of the subjects who reached step 1 that also reached this step.
    rate_from_first: float
    #: Share of the previous step's subjects that reached this one.
    rate_from_previous: float
    #: Subjects who reached the previous step and not this one.
    dropoff: int
    #: Same step in the comparison period, when one was requested.
    previous_count: int | None = None
    delta: int | None = None


@dataclass
class FunnelReport:
    """The whole answer: steps, period, totals, and whether it is complete."""

    slug: str
    steps: list = field(default_factory=list)
    entered: int = 0
    completed: int = 0
    conversion: float = 0.0
    window_seconds: int = 0
    start: datetime | None = None
    end: datetime | None = None
    compare_start: datetime | None = None
    compare_end: datetime | None = None
    scanned: int = 0
    truncated: bool = False


def _default_window() -> int:
    from .conf import analytics_settings

    return int(analytics_settings.DEFAULT_FUNNEL_WINDOW_SECONDS or 604800)


def declared_funnels() -> dict[str, FunnelSpec]:
    """Funnels from ``STAPEL_ANALYTICS["FUNNELS"]`` (the project spec)."""
    from .conf import analytics_settings

    raw = analytics_settings.FUNNELS or {}
    if not isinstance(raw, dict):
        raise TypeError(
            f"STAPEL_ANALYTICS['FUNNELS'] must be a mapping of slug -> funnel, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, FunnelSpec] = {}
    for slug, definition in raw.items():
        if definition is None:
            continue
        if not isinstance(definition, dict):
            raise TypeError(
                f"declared funnel {slug!r} must be an object, "
                f"got {type(definition).__name__}"
            )
        # `or` would turn an explicit 0 (= unbounded window) into the
        # default, which is the opposite of what the author wrote.
        window = definition.get("window_seconds")
        out[str(slug)] = FunnelSpec(
            slug=str(slug),
            steps=list(definition.get("steps") or []),
            window_seconds=int(_default_window() if window is None else window),
            title=str(definition.get("title") or ""),
            description=str(definition.get("description") or ""),
            source="settings",
            is_active=bool(definition.get("is_active", True)),
        )
    return out


def _spec_from_row(row) -> FunnelSpec:
    return FunnelSpec(
        slug=row.slug,
        steps=list(row.steps or []),
        # 0 is a legal value meaning "no window"; it must survive the read.
        window_seconds=int(row.window_seconds or 0),
        title=row.title,
        description=row.description,
        source="db",
        is_active=row.is_active,
        id=str(row.id),
        owner_id=str(row.owner_id) if row.owner_id else None,
        workspace_id=str(row.workspace_id) if row.workspace_id else None,
    )


def list_funnels(*, owner_id=None, workspace_id=None, include_declared: bool = True):
    """Authored funnels (optionally scoped) merged over declared ones.

    Scoping filters only the DB rows: a declared funnel belongs to the
    deployment's spec, not to a person, so hiding it from its own operator
    because they did not type it in would be a scope that lies.
    """
    from .models import Funnel

    merged: dict[str, FunnelSpec] = declared_funnels() if include_declared else {}
    queryset = Funnel.objects.all()
    if owner_id is not None:
        queryset = queryset.filter(owner_id=owner_id)
    if workspace_id is not None:
        queryset = queryset.filter(workspace_id=workspace_id)
    for row in queryset:
        merged[row.slug] = _spec_from_row(row)
    return [merged[slug] for slug in sorted(merged)]


def resolve_funnel(slug: str) -> FunnelSpec:
    """The funnel *slug* names: an authored row first, else a declared one."""
    from .models import Funnel

    row = Funnel.objects.filter(slug=slug).first()
    if row is not None:
        return _spec_from_row(row)
    declared = declared_funnels().get(slug)
    if declared is not None:
        return declared
    raise UnknownFunnel(slug)


# ── Computation ──────────────────────────────────────────────────────


def _subject_of(row: dict):
    from .conf import analytics_settings

    return analytics_settings.SUBJECT_RESOLVER(row)


def _collect(steps: list, start, end, extra_filters: dict | None, budget: int):
    """``(per_subject, scanned, truncated)`` for one period.

    ``per_subject[subject][step_index] = earliest ts``. One pass, one dict:
    the stream is read once and the ordering work happens in memory, because
    the alternative is N queries for N steps and a race between them.
    """
    from .store import iter_events

    wanted = {name: index for index, name in enumerate(steps)}
    per_subject: dict[str, dict[int, datetime]] = {}
    scanned = 0
    truncated = False
    for row in iter_events(time_range=(start, end), filters=extra_filters, limit=budget):
        scanned += 1
        if scanned >= budget:
            truncated = True
        index = wanted.get(row.get("name"))
        if index is None:
            continue
        subject = _subject_of(row)
        if not subject:
            # An event with no subject at all cannot be part of a
            # conversion: it is a fact about the deployment, not a person.
            continue
        seen = per_subject.setdefault(subject, {})
        ts = row.get("ts")
        if index not in seen or ts < seen[index]:
            seen[index] = ts
    return per_subject, scanned, truncated


def _count_steps(per_subject: dict, steps: list, window_seconds: int) -> list[int]:
    """Subjects reaching each step, in order, honouring the window."""
    counts = [0] * len(steps)
    window = timedelta(seconds=int(window_seconds or 0))
    for seen in per_subject.values():
        first = seen.get(0)
        if first is None:
            continue
        counts[0] += 1
        previous = first
        deadline = first + window if window else None
        for index in range(1, len(steps)):
            ts = seen.get(index)
            if ts is None or ts < previous:
                break
            if deadline is not None and ts > deadline:
                break
            counts[index] += 1
            previous = ts
    return counts


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def report(
    spec: FunnelSpec,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    compare: bool = False,
    filters: dict | None = None,
) -> FunnelReport:
    """Compute *spec* over ``[start, end)``, optionally against the period before.

    The comparison period is the SAME LENGTH immediately preceding the
    requested one. That is the only definition that keeps "vs. previous"
    honest when somebody asks for eleven days.
    """
    from django.utils import timezone

    from .conf import analytics_settings

    steps = list(spec.steps or [])
    if not steps:
        raise ValueError(f"funnel {spec.slug!r} has no steps")

    end = end or timezone.now()
    if start is None:
        start = end - timedelta(seconds=int(spec.window_seconds or _default_window()))
    if start >= end:
        raise ValueError("the report period is empty (start >= end)")

    budget = int(analytics_settings.MAX_REPORT_EVENTS or 200000)
    per_subject, scanned, truncated = _collect(steps, start, end, filters, budget)
    counts = _count_steps(per_subject, steps, spec.window_seconds)

    previous_counts: list[int] | None = None
    compare_start = compare_end = None
    if compare:
        length = end - start
        compare_end, compare_start = start, start - length
        prior, prior_scanned, prior_truncated = _collect(
            steps, compare_start, compare_end, filters, budget
        )
        previous_counts = _count_steps(prior, steps, spec.window_seconds)
        scanned += prior_scanned
        truncated = truncated or prior_truncated

    results: list[StepResult] = []
    for index, name in enumerate(steps):
        count = counts[index]
        previous = counts[index - 1] if index else count
        results.append(
            StepResult(
                name=name,
                count=count,
                rate_from_first=_rate(count, counts[0]),
                rate_from_previous=_rate(count, previous) if index else 1.0 if count else 0.0,
                dropoff=max(previous - count, 0) if index else 0,
                previous_count=None if previous_counts is None else previous_counts[index],
                delta=(
                    None if previous_counts is None else count - previous_counts[index]
                ),
            )
        )

    return FunnelReport(
        slug=spec.slug,
        steps=results,
        entered=counts[0],
        completed=counts[-1],
        conversion=_rate(counts[-1], counts[0]),
        window_seconds=int(spec.window_seconds or 0),
        start=start,
        end=end,
        compare_start=compare_start,
        compare_end=compare_end,
        scanned=scanned,
        truncated=truncated,
    )


def funnel_report(slug: str, **kwargs) -> FunnelReport:
    """Resolve *slug* and compute its report — the one-call entry point."""
    return report(resolve_funnel(slug), **kwargs)


__all__ = [
    "FunnelReport",
    "FunnelSpec",
    "StepResult",
    "UnknownFunnel",
    "declared_funnels",
    "funnel_report",
    "list_funnels",
    "report",
    "resolve_funnel",
]
