"""Single-module Django settings for stapel-analytics.

One ``settings.configure(...)`` block serves the test suite and the
migration and management harnesses, which is the point: they cannot drift
apart if there is nothing to drift.
"""
from __future__ import annotations


def settings_kwargs(*, root_urlconf: str = "stapel_analytics.tests.urls") -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module instance."""
    return dict(
        # Long enough for stapel_core.prodguard.E001: the harness is a
        # host, and a host whose own checks fail cannot vouch for a
        # module's checks.
        SECRET_KEY="test-secret-key-not-for-production-0123456789abcdef",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            # The event store's table: analytics rows live here, not in a
            # table of this module's own (store.py). Installing it is a
            # HOST requirement, and analytics.E001 says so when it is
            # missing — the suite mounts it because the suite is the host.
            "stapel_core.django.eventstore",
            "rest_framework",
            "drf_spectacular",
            "stapel_analytics",
        ],
        # django.contrib.admin refuses to boot without these three
        # (admin.E408-E410), and the module registers a ModelAdmin.
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                    ]
                },
            }
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Write-through event store: reads flush anyway, but a test that
        # asserts on a row right after an append should not depend on the
        # buffer's size threshold to see it.
        STAPEL_EVENTSTORE={"BUFFER_SYNC": True},
        # Synchronous in-process comm with schema validation ON, so the
        # committed contracts in schemas/ are enforced by the tests.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
            # An emit outside a transaction is a bug here, not a warning:
            # the outbox canon is what makes "the rows exist" and "the
            # batch was announced" one decision.
            "EMIT_OUTSIDE_ATOMIC": "error",
        },
        MIGRATION_MODULES={
            "users": None,
        },
    )
