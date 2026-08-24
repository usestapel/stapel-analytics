"""Presenters for stapel-analytics — the DTO-building layer (§55).

Presenter discipline (enforced by SWAP001/SWAP002 in ``stapel-verify``):
views NEVER instantiate a ``dto.py`` dataclass directly — every DTO is built
by a presenter resolved through ``get_presenter(KEY, default=...)``, so a
host project can reshape any envelope via ``STAPEL_SWAP`` without forking
this module.

Only one of these presenters is model-backed (:class:`FunnelPresenter`).
The rest present COMPUTED things — an ingest receipt, the registry, a
conversion report — and are plain classes with a ``present()``. That
asymmetry is the module's shape showing through: its main table is not its
own (``store.py``), so most of what it returns is arithmetic rather than
rows.
"""
from __future__ import annotations

from stapel_core.django.api.presenters import Presenter, PresenterField
from stapel_core.django.swappable import declare_swap, get_presenter

from .dto import (
    EventDefinition,
    EventRegistry,
    EventsReport,
    FunnelDefinition,
    FunnelReportDTO,
    FunnelStep,
    IngestReceipt,
    RollupBucket,
)
from .models import Funnel

FUNNEL_PRESENTER_KEY = "ANALYTICS_FUNNEL_PRESENTER"
DEFAULT_FUNNEL_PRESENTER = "stapel_analytics.presenters.FunnelPresenter"
RECEIPT_PRESENTER_KEY = "ANALYTICS_RECEIPT_PRESENTER"
DEFAULT_RECEIPT_PRESENTER = "stapel_analytics.presenters.ReceiptPresenter"
REGISTRY_PRESENTER_KEY = "ANALYTICS_REGISTRY_PRESENTER"
DEFAULT_REGISTRY_PRESENTER = "stapel_analytics.presenters.RegistryPresenter"
REPORT_PRESENTER_KEY = "ANALYTICS_REPORT_PRESENTER"
DEFAULT_REPORT_PRESENTER = "stapel_analytics.presenters.ReportPresenter"

declare_swap(FUNNEL_PRESENTER_KEY, DEFAULT_FUNNEL_PRESENTER)
declare_swap(RECEIPT_PRESENTER_KEY, DEFAULT_RECEIPT_PRESENTER)
declare_swap(REGISTRY_PRESENTER_KEY, DEFAULT_REGISTRY_PRESENTER)
declare_swap(REPORT_PRESENTER_KEY, DEFAULT_REPORT_PRESENTER)


class FunnelPresenter(Presenter):
    """Presents an authored funnel row.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "slug": "checkout",
            "title": "Checkout",
            "description": "",
            "steps": ["flow.checkout.started", "payment_completed"],
            "window_seconds": 604800,
            "is_active": true,
            "source": "db"
        }
    """

    model = Funnel
    fields = ("slug", "title", "description", "window_seconds", "is_active")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        # A JSONField deduces to ``Any``, which the dataclass serializer
        # refuses to build a field for — the shape is declared, not inferred.
        "steps": PresenterField(
            type=list,
            source=lambda dao: list(dao.steps or []),
            help_text="Ordered event names a subject must pass through.",
        ),
        "source": PresenterField(
            type=str,
            source=lambda dao: "db",
            help_text="'db' (authored here) or 'settings' (declared in the "
                      "project spec and read-only over the API).",
        ),
    }


def get_funnel_presenter() -> type:
    return get_presenter(FUNNEL_PRESENTER_KEY, default=DEFAULT_FUNNEL_PRESENTER)


def present_funnel_spec(spec) -> FunnelDefinition:
    """A :class:`funnels.FunnelSpec` — authored OR declared — as a DTO.

    Listing goes through this rather than through the model presenter,
    because a declared funnel has no row to present and hiding it from the
    listing would make "my funnels" disagree with "the funnels that report".
    """
    return FunnelDefinition(
        slug=spec.slug,
        steps=list(spec.steps or []),
        window_seconds=int(spec.window_seconds or 0),
        title=spec.title,
        description=spec.description,
        source=spec.source,
        is_active=spec.is_active,
        id=spec.id,
    )


class ReceiptPresenter:
    """Builds the ingest receipt — the only place IngestReceipt is made."""

    def present(self, result: dict) -> IngestReceipt:
        return IngestReceipt(
            accepted=int(result.get("accepted") or 0),
            rejected=list(result.get("rejected") or []),
            unregistered=list(result.get("unregistered") or []),
            source=str(result.get("source") or ""),
        )


def get_receipt_presenter() -> type:
    return get_presenter(RECEIPT_PRESENTER_KEY, default=DEFAULT_RECEIPT_PRESENTER)


class RegistryPresenter:
    """Builds the event-registry envelope."""

    def present(self) -> EventRegistry:
        from .actions import bridged_actions
        from .adapters import active_adapters
        from .registry import BUILTIN_EVENTS, event_registry, registry_mode

        definitions = []
        for name, entry in sorted(event_registry().items()):
            definitions.append(
                EventDefinition(
                    name=name,
                    description=str(entry.get("description") or ""),
                    props=dict(entry.get("props") or {}),
                    flow=entry.get("flow") or entry.get("funnel") or None,
                    builtin=name in BUILTIN_EVENTS,
                )
            )
        return EventRegistry(
            events=definitions,
            mode=registry_mode(),
            adapters=sorted(active_adapters()),
            bridged_actions=sorted(bridged_actions()),
        )


def get_registry_presenter() -> type:
    return get_presenter(REGISTRY_PRESENTER_KEY, default=DEFAULT_REGISTRY_PRESENTER)


class ReportPresenter:
    """Builds the funnel and events report envelopes."""

    def present(self, report) -> FunnelReportDTO:
        return FunnelReportDTO(
            slug=report.slug,
            steps=[
                FunnelStep(
                    name=step.name,
                    count=step.count,
                    rate_from_first=step.rate_from_first,
                    rate_from_previous=step.rate_from_previous,
                    dropoff=step.dropoff,
                    previous_count=step.previous_count,
                    delta=step.delta,
                )
                for step in report.steps
            ],
            entered=report.entered,
            completed=report.completed,
            conversion=report.conversion,
            window_seconds=report.window_seconds,
            start=report.start,
            end=report.end,
            compare_start=report.compare_start,
            compare_end=report.compare_end,
            scanned=report.scanned,
            truncated=report.truncated,
        )

    def present_events(self, rows, *, group_by, start, end) -> EventsReport:
        buckets = [
            RollupBucket(group=dict(row.group), count=int(row.count))
            for row in sorted(rows, key=lambda r: -r.count)
        ]
        return EventsReport(
            buckets=buckets,
            total=sum(bucket.count for bucket in buckets),
            group_by=list(group_by),
            start=start,
            end=end,
        )


def get_report_presenter() -> type:
    return get_presenter(REPORT_PRESENTER_KEY, default=DEFAULT_REPORT_PRESENTER)


__all__ = [
    "DEFAULT_FUNNEL_PRESENTER",
    "DEFAULT_RECEIPT_PRESENTER",
    "DEFAULT_REGISTRY_PRESENTER",
    "DEFAULT_REPORT_PRESENTER",
    "FUNNEL_PRESENTER_KEY",
    "FunnelPresenter",
    "RECEIPT_PRESENTER_KEY",
    "REGISTRY_PRESENTER_KEY",
    "REPORT_PRESENTER_KEY",
    "ReceiptPresenter",
    "RegistryPresenter",
    "ReportPresenter",
    "get_funnel_presenter",
    "get_receipt_presenter",
    "get_registry_presenter",
    "get_report_presenter",
    "present_funnel_spec",
]
