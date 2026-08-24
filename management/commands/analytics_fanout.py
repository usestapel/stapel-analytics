"""Re-deliver stored events to the fan-out adapters over a time range.

The recovery path for the one failure the fan-out design accepts: an
adapter's outage is CONTAINED (the event store is the record, the mirror is
best-effort), so getting the mirror back in sync is an explicit, bounded,
idempotent-by-range operation rather than an unbounded retry queue.
"""
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Re-run server-side fan-out for events in a time range."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="ISO-8601 lower bound. Default: 1 hour ago.")
        parser.add_argument("--until", help="ISO-8601 upper bound. Default: now.")
        parser.add_argument("--adapter", help="Only this adapter (default: all active).")
        parser.add_argument("--batch-size", type=int, default=200)

    def handle(self, *args, **options):
        from stapel_analytics.adapters import fan_out
        from stapel_analytics.store import iter_events

        def _parse(value, fallback):
            if not value:
                return fallback
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        until = _parse(options.get("until"), timezone.now())
        since = _parse(options.get("since"), until - timedelta(hours=1))
        if since >= until:
            raise CommandError("--since must be before --until")

        size = max(int(options["batch_size"]), 1)
        batch, total, results = [], 0, {}
        for row in iter_events(time_range=(since, until)):
            ts = row.pop("ts", None)
            batch.append({**row, "ts": ts.isoformat() if ts is not None else None})
            if len(batch) >= size:
                total += self._deliver(fan_out, batch, options.get("adapter"), results)
                batch = []
        if batch:
            total += self._deliver(fan_out, batch, options.get("adapter"), results)

        if not total:
            self.stdout.write("no events in range")
            return
        self.stdout.write(f"{total} event(s) re-delivered")
        for name, counts in sorted(results.items()):
            error = counts.get("error")
            self.stdout.write(
                f"  {name:<24} {counts['delivered']}"
                + (f"  ERROR: {error}" if error else "")
            )

    def _deliver(self, fan_out, batch, only, results):
        from stapel_analytics.adapters import UnknownAdapter

        try:
            outcome = fan_out(batch, only=only)
        except UnknownAdapter as exc:
            raise CommandError(f"unknown adapter {exc}") from exc
        for name, counts in outcome.items():
            bucket = results.setdefault(name, {"delivered": 0})
            bucket["delivered"] += counts.get("delivered", 0)
            if counts.get("error"):
                bucket["error"] = counts["error"]
        return len(batch)
