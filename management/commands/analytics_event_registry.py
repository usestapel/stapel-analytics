"""Print the event vocabulary this deployment admits.

The same answer the ``analytics.event_registry`` comm Function and the
``/event-registry`` endpoint give — available before either is reachable,
which is when an operator most wants it ("is my events.json actually being
read here?").
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "List every registered analytics event (built-ins + events.json + settings)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **options):
        from stapel_analytics.registry import BUILTIN_EVENTS, event_registry, registry_mode

        registry = event_registry()
        rows = [
            {
                "event": name,
                "builtin": name in BUILTIN_EVENTS,
                "flow": entry.get("flow") or entry.get("funnel") or "",
                "description": str(entry.get("description") or ""),
                "props": sorted(entry.get("props") or {}),
            }
            for name, entry in sorted(registry.items())
        ]
        if options["json"]:
            self.stdout.write(json.dumps({"mode": registry_mode(), "events": rows}, indent=2))
            return
        if not rows:
            self.stdout.write("the registry is empty")
            return
        for row in rows:
            mark = "*" if row["builtin"] else " "
            self.stdout.write(f"{mark} {row['event']:<44} {row['flow']}")
        self.stdout.write(
            f"\n{len(rows)} event(s), mode={registry_mode()} "
            "(* = built-in)"
        )
