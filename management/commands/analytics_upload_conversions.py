"""Drain the offline click-conversion outbox.

The retry half of ``analytics.upload_click_conversion``. The comm function
tries once, inline, because a caller that can be told "uploaded" should be;
everything that could not be attempted then — an expired token, a quota, an
outage — is a row this command picks up later on the configured backoff.

``--dry-run`` writes NOTHING: no status, no reason, no attempt counter, no
next-attempt stamp. That is the point of it. An operator running a dry run
is asking "what would go out", and a dry run that bumped a counter would
consume an attempt from the budget that decides when a row is given up on —
the one number the answer depends on.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Upload pending offline click conversions to Google Ads."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=100,
            help="Rows to consider this pass (default: 100).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List the rows that are due and change nothing.",
        )

    def handle(self, *args, **options):
        from stapel_analytics import conversions

        limit = max(int(options["limit"]), 0)
        rows = conversions.due(limit)
        if not rows:
            self.stdout.write("no conversion uploads are due")
            return

        if options["dry_run"]:
            configured = conversions.is_configured()
            self.stdout.write(
                f"{len(rows)} conversion upload(s) due"
                + ("" if configured else " — Google Ads credentials are INCOMPLETE")
            )
            for row in rows:
                self.stdout.write(
                    f"  {row.pk:<8} {row.click_id_type:<7} "
                    f"{row.click_id[:24]:<24} {row.conversion_at:%Y-%m-%d %H:%M} "
                    f"attempts={row.attempts}"
                    + (f"  last: {row.reason}" if row.reason else "")
                )
            self.stdout.write("dry run — nothing was written")
            return

        counts = {}
        for row in rows:
            answer = conversions.deliver(row)
            counts[answer["status"]] = counts.get(answer["status"], 0) + 1
        self.stdout.write(f"{len(rows)} conversion upload(s) attempted")
        for status, count in sorted(counts.items()):
            self.stdout.write(f"  {status:<10} {count}")
