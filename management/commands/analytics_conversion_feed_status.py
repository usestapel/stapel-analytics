"""Has the conversion feed been read yet, and what was in it.

The one question a pull cannot answer from the outside. An operator can
curl the URL themselves and prove the endpoint works; what they cannot see
is whether the ad platform's own scheduler has ever come — and that is the
fact that gates a decision on the other side of the fence (switching a
browser-side conversion to secondary the moment the offline import starts
landing, and not one day earlier, or payments stop being counted at all).

Reads. Writes nothing, serves nothing, and deliberately never prints the
token: the answer to "is it configured" is a yes or a no, and a command
that echoed the secret would put it in a scrollback, a CI log and a
screenshot.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Show the conversion feed's configuration and its last fetch."

    def handle(self, *args, **options):
        from stapel_analytics import feed

        status = feed.feed_status()

        if not status["enabled"]:
            self.stdout.write(
                "conversion feed: DISABLED — no CONVERSION_FEED_TOKEN is set, "
                "and the endpoint answers 404"
            )
        else:
            self.stdout.write("conversion feed: enabled")
        self.stdout.write(f"  conversion name   {status['conversion_name'] or '(unset)'}")
        self.stdout.write(f"  feed window       {status['window_days']} day(s)")
        self.stdout.write(f"  rows in the feed  {status['rows_available']}")
        self.stdout.write(f"  outbox pending    {status['pending']}")

        if status["last_fetch_at"] is None:
            self.stdout.write(
                "  last fetch        NEVER — nothing has read this feed yet"
            )
            return
        self.stdout.write(f"  last fetch        {status['last_fetch_at']}")
        self.stdout.write(f"  rows served       {status['last_fetch_rows']}")
        self.stdout.write(
            f"  remote            {status['last_fetch_remote'] or '(not forwarded)'}"
        )
        self.stdout.write(
            f"  fetches on record {status['fetches']} "
            f"(last {feed.FETCH_HISTORY_DAYS} days)"
        )
