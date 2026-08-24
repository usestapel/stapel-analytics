"""Compute one funnel's conversion from a shell.

Same numbers as ``GET /funnels/<slug>/report`` and the
``analytics.funnel_report`` Function — reachable from a container that has
no HTTP exposure, which is where an operator debugs "why is this funnel
flat".
"""
import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Print conversion by step for one funnel."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--start", help="ISO-8601 lower bound (inclusive).")
        parser.add_argument("--end", help="ISO-8601 upper bound (exclusive).")
        parser.add_argument("--compare", action="store_true",
                            help="Also compute the equal-length period before.")
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **options):
        from stapel_analytics.funnels import UnknownFunnel, funnel_report

        def _parse(value):
            if not value:
                return None
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        try:
            report = funnel_report(
                options["slug"],
                start=_parse(options.get("start")),
                end=_parse(options.get("end")),
                compare=bool(options.get("compare")),
            )
        except UnknownFunnel as exc:
            raise CommandError(f"no funnel answers to {exc}") from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps({
                "slug": report.slug,
                "entered": report.entered,
                "completed": report.completed,
                "conversion": report.conversion,
                "truncated": report.truncated,
                "steps": [
                    {
                        "name": step.name,
                        "count": step.count,
                        "rate_from_first": step.rate_from_first,
                        "rate_from_previous": step.rate_from_previous,
                        "dropoff": step.dropoff,
                        "previous_count": step.previous_count,
                        "delta": step.delta,
                    }
                    for step in report.steps
                ],
            }, indent=2))
            return

        self.stdout.write(f"{report.slug}  {report.start:%Y-%m-%d} .. {report.end:%Y-%m-%d}")
        for step in report.steps:
            delta = "" if step.delta is None else f"  ({step.delta:+d} vs previous)"
            self.stdout.write(
                f"  {step.name:<40} {step.count:>8}  "
                f"{step.rate_from_first * 100:6.2f}% of entry"
                f"{'' if step.dropoff == 0 else f'  -{step.dropoff}'}{delta}"
            )
        self.stdout.write(
            f"\nconversion {report.conversion * 100:.2f}% "
            f"({report.completed}/{report.entered})"
            + ("  [TRUNCATED — widen MAX_REPORT_EVENTS]" if report.truncated else "")
        )
