"""stapel-analytics — the backend half of the Stapel analytics standard.

The frontend facade shipped first (``@stapel/analytics``: ``track`` /
``identify`` / ``page``, consent gate, offline queue, provider fan-out).
This is what it talks to, and what the rest of the fleet talks to:

- **an event registry** — the deployment's vocabulary, merged from the
  built-ins, the project's ``analytics/events.json`` and settings. An
  unregistered event is stored and MARKED, never lost;
- **ingest** — ``POST /analytics/api/v1/events``, the wire format
  ``@stapel/analytics`` already sends, with a PII guard that refuses rather
  than warns;
- **funnels** — conversion by step over a window, with a comparison period;
  the numbers Studio shows a client and a CTO agent reads;
- **a comm bridge** — a host's Actions (a payment, an email, a webhook
  delivery) become steps of the same funnels as the clicks that led to them;
- **server-side fan-out** — an open adapter registry, delivered through the
  comm outbox rather than on the ingest request thread.

Rows go to ``stapel_core.eventstore``; this module owns exactly one table
(``Funnel``). Analytics rows are user data, so the erasure provider ships in
the same release as the ingest.

Public API (lazily exported, PEP 562 — importing this package never pulls in
Django or requires configured settings):

- ``analytics_settings`` — resolved app settings (``stapel_analytics.conf``);
- ``track`` — record one server-side event (``services.track``);
- ``event_registry`` / ``register_event`` — the vocabulary and its seam;
- ``register_adapter`` — add/override/remove a fan-out adapter at runtime;
- ``funnel_report`` — conversion by step for one funnel.
"""

__all__ = [
    "analytics_settings",
    "event_registry",
    "funnel_report",
    "register_adapter",
    "register_event",
    "track",
]

# name -> submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_analytics` stays Django-free.
_LAZY_EXPORTS = {
    "analytics_settings": ".conf",
    "event_registry": ".registry",
    "register_event": ".registry",
    "register_adapter": ".adapters",
    "funnel_report": ".funnels",
    "track": ".services",
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        value = getattr(import_module(_LAZY_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
