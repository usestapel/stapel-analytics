"""Admin for stapel-analytics.

Only the funnel is registered, because only the funnel is a row of this
app's own. The events live in ``stapel_core.django.eventstore``'s
append-only table and are administered there (as an ``@access.ops``
journal), which is the right place: they are not this module's data model,
they are its storage.
"""
from django.contrib import admin

from .models import Funnel


@admin.register(Funnel)
class FunnelAdmin(admin.ModelAdmin):
    list_display = ("slug", "title", "step_count", "window_seconds", "is_active",
                    "workspace_id", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("slug", "title", "description")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("slug",)

    @admin.display(description="Steps")
    def step_count(self, obj):
        return len(obj.steps or [])


__all__ = ["FunnelAdmin"]
