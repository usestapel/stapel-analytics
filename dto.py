"""Dataclass DTOs — the API models of stapel-analytics (never ORM instances)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class IngestReceipt:
    """What the ingest endpoint answers.

    ``rejected`` is a LIST OF REASONS, not a count: a collector that learns
    "3 of 20 refused" and not why has no way to stop sending the same three
    forever, and the facade's retry ladder guarantees it will.
    """

    accepted: int
    rejected: List[dict] = field(default_factory=list)
    unregistered: List[str] = field(default_factory=list)
    source: str = ""


@dataclass
class EventDefinition:
    """One registry entry, in the shape ``events.json`` uses."""

    name: str
    description: str = ""
    props: dict = field(default_factory=dict)
    flow: Optional[str] = None
    builtin: bool = False


@dataclass
class EventRegistry:
    """The vocabulary this deployment admits, plus how it enforces it."""

    events: List[EventDefinition] = field(default_factory=list)
    mode: str = "warn"
    adapters: List[str] = field(default_factory=list)
    bridged_actions: List[str] = field(default_factory=list)


@dataclass
class FunnelDefinition:
    """A funnel as the API presents it."""

    slug: str
    steps: List[str] = field(default_factory=list)
    window_seconds: int = 0
    title: str = ""
    description: str = ""
    source: str = "db"
    is_active: bool = True
    id: Optional[str] = None


@dataclass
class FunnelStep:
    """One step of a computed report."""

    name: str
    count: int
    rate_from_first: float
    rate_from_previous: float
    dropoff: int
    previous_count: Optional[int] = None
    delta: Optional[int] = None


@dataclass
class FunnelReportDTO:
    """Conversion by step over a period, optionally versus the one before."""

    slug: str
    steps: List[FunnelStep] = field(default_factory=list)
    entered: int = 0
    completed: int = 0
    conversion: float = 0.0
    window_seconds: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    compare_start: Optional[datetime] = None
    compare_end: Optional[datetime] = None
    scanned: int = 0
    truncated: bool = False


@dataclass
class RollupBucket:
    """One group-by bucket of the events report."""

    group: dict
    count: int


@dataclass
class EventsReport:
    """Counts grouped by a payload field over a period — the dashboard's
    cheap half (``store.rollup``)."""

    buckets: List[RollupBucket] = field(default_factory=list)
    total: int = 0
    group_by: List[str] = field(default_factory=list)
    start: Optional[datetime] = None
    end: Optional[datetime] = None


__all__ = [
    "EventDefinition",
    "EventRegistry",
    "EventsReport",
    "FunnelDefinition",
    "FunnelReportDTO",
    "FunnelStep",
    "IngestReceipt",
    "RollupBucket",
]
