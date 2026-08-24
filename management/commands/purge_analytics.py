"""Apply the analytics retention horizon.

Rows keyed to people are kept for ``STAPEL_ANALYTICS['RETENTION_DAYS']`` and
then go. With the key unset this command is a deliberate no-op and says so —
"keep forever" is a decision, and a command that silently did nothing would
hide the day somebody made it by accident (``analytics.W005``).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete analytics events older than the configured retention."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int,
                            help="Override STAPEL_ANALYTICS['RETENTION_DAYS'].")

    def handle(self, *args, **options):
        from datetime import timedelta

        from django.utils import timezone

        from stapel_analytics import services
        from stapel_analytics.conf import analytics_settings

        days = options.get("days") or analytics_settings.RETENTION_DAYS
        if not days:
            self.stdout.write(
                "no retention horizon configured (STAPEL_ANALYTICS['RETENTION_DAYS'] "
                "is unset) — nothing purged"
            )
            return
        older_than = timezone.now() - timedelta(days=int(days))
        removed = services.purge_events(older_than=older_than)
        self.stdout.write(
            f"{removed} analytics event(s) older than {older_than:%Y-%m-%d} removed"
        )
