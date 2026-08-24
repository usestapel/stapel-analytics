"""The storage seam — this module's whole relationship with the event store."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_analytics import services, store
from stapel_analytics.ingest import NormalizedEvent

DECLARED = {"EVENTS": {"a.b": {"description": "x"}, "c.d": {"description": "y"}}}


@pytest.fixture(autouse=True)
def _declared(settings):
    settings.STAPEL_ANALYTICS = dict(DECLARED)


def event(name="a.b", *, at=None, **payload):
    return NormalizedEvent(name=name, kind="track", ts=at or timezone.now(),
                           **payload)


class TestStreamName:
    def test_the_default_stream_is_analytics(self):
        assert store.stream_name() == "analytics"

    def test_it_is_configurable(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "STREAM": "product-events"}
        assert store.stream_name() == "product-events"


@pytest.mark.django_db
class TestAppend:
    def test_appending_returns_the_count(self):
        assert store.append([event(), event()]) == 2

    def test_appending_nothing_is_a_no_op(self):
        assert store.append([]) == 0

    def test_the_payload_is_what_is_stored(self):
        store.append([event(props={"k": 1}, user_hash="h")])
        row = next(iter(store.iter_events()))
        assert row["props"] == {"k": 1} and row["user_hash"] == "h"

    def test_the_timestamp_is_injected_on_read(self):
        moment = timezone.now() - timedelta(hours=1)
        store.append([event(at=moment)])
        row = next(iter(store.iter_events()))
        assert abs((row["ts"] - moment).total_seconds()) < 1


@pytest.mark.django_db
class TestIterEvents:
    def test_it_walks_every_row(self):
        store.append([event() for _ in range(5)])
        assert len(list(store.iter_events())) == 5

    def test_it_pages_through_the_cursor(self, settings):
        settings.STAPEL_ANALYTICS = {**DECLARED, "QUERY_PAGE_SIZE": 2}
        base = timezone.now() - timedelta(hours=1)
        store.append([event(at=base + timedelta(seconds=i)) for i in range(5)])
        assert len(list(store.iter_events())) == 5

    def test_the_limit_bounds_the_walk(self):
        base = timezone.now() - timedelta(hours=1)
        store.append([event(at=base + timedelta(seconds=i)) for i in range(5)])
        assert len(list(store.iter_events(limit=3))) == 3

    def test_a_zero_limit_yields_nothing(self):
        store.append([event()])
        assert list(store.iter_events(limit=0)) == []

    def test_a_time_range_bounds_the_walk(self):
        old = timezone.now() - timedelta(days=2)
        store.append([event(at=old), event()])
        recent = list(store.iter_events(
            time_range=(timezone.now() - timedelta(hours=1), None)
        ))
        assert len(recent) == 1

    def test_a_payload_filter_narrows_the_walk(self):
        store.append([event(user_hash="a"), event(user_hash="b")])
        assert len(list(store.iter_events(filters={"user_hash": "a"}))) == 1

    def test_rows_come_back_oldest_first(self):
        base = timezone.now() - timedelta(hours=1)
        store.append([
            event(name="c.d", at=base + timedelta(minutes=1)),
            event(name="a.b", at=base),
        ])
        assert [row["name"] for row in store.iter_events()] == ["a.b", "c.d"]


@pytest.mark.django_db
class TestRollup:
    def test_it_groups_by_a_payload_key(self):
        store.append([event(name="a.b"), event(name="a.b"), event(name="c.d")])
        rows = {row.group["name"]: row.count for row in store.rollup(group_by=["name"])}
        assert rows == {"a.b": 2, "c.d": 1}

    def test_a_time_range_bounds_it(self):
        store.append([event(at=timezone.now() - timedelta(days=2)), event()])
        rows = store.rollup(
            group_by=["name"],
            time_range=(timezone.now() - timedelta(hours=1), None),
        )
        assert sum(row.count for row in rows) == 1


@pytest.mark.django_db
class TestPurge:
    def test_a_time_bound_purges(self):
        store.append([event(at=timezone.now() - timedelta(days=10)), event()])
        removed = store.purge(older_than=timezone.now() - timedelta(days=1))
        assert removed == 1

    def test_a_filter_purges_one_subject(self):
        store.append([event(user_hash="a"), event(user_hash="b")])
        assert store.purge(filters={"user_hash": "a"}) == 1
        assert len(list(store.iter_events())) == 1

    def test_an_unbounded_purge_is_refused(self):
        """'Delete the analytics of everybody who ever used this deployment'
        is not something any caller here means."""
        store.append([event()])
        with pytest.raises(ValueError):
            store.purge()


@pytest.mark.django_db
class TestBackendSeam:
    def test_the_default_backend_is_recognised(self):
        assert store.uses_default_backend() is True

    def test_a_routed_stream_is_not_the_default_backend(self, settings):
        settings.STAPEL_EVENTSTORE = {
            "ROUTES": {"analytics": "stapel_analytics.tests.test_checks.FakeStore"},
            "BUFFER_SYNC": True,
        }
        assert store.uses_default_backend() is False

    def test_flush_is_callable(self):
        services.track("a.b", {})
        store.flush()
        assert len(list(store.iter_events())) == 1


class TestSeamIsolation:
    def test_only_store_py_imports_the_event_store_api(self):
        """One file to read on the day a deployment needs another store.

        Parsed rather than grepped: half the package MENTIONS the seam in a
        docstring, and a docstring is not a dependency. What matters is the
        import graph — ``checks.py`` is the one legitimate exception, and it
        imports the store's SETTINGS (a retention horizon), never its API.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        allowed = {"store.py", "conftest.py", "_codegen_settings.py", "checks.py"}
        offenders = []
        for path in sorted(root.glob("*.py")):
            if path.name in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = {alias.name for alias in node.names}
                    if module.startswith("stapel_core.eventstore") or (
                        module == "stapel_core" and "eventstore" in names
                    ):
                        offenders.append(f"{path.name}: from {module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("stapel_core.eventstore"):
                            offenders.append(f"{path.name}: import {alias.name}")
        assert offenders == []

    def test_checks_only_reads_the_stores_settings(self):
        """The one allowed exception, pinned so it stays an exception."""
        import ast
        import pathlib

        path = pathlib.Path(__file__).resolve().parent.parent / "checks.py"
        modules = {
            node.module
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("stapel_core.eventstore")
        }
        assert modules == {"stapel_core.eventstore.conf"}
