#!/usr/bin/env python
"""Single-module management entry point.

Reads the same ``settings.configure(...)`` block the test suite does
(``_codegen_settings``), so ``makemigrations``, ``check`` and this module's
own commands cannot drift from what the tests exercise.
"""
import sys


def main() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        from stapel_analytics._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
    django.setup()

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
