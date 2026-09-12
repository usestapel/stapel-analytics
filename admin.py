"""Admin for stapel-analytics.

Three models, because three are all an operator asks about. The events live in
``stapel_core.django.eventstore``'s append-only table and are administered
there (as an ``@access.ops`` journal), which is the right place: they are
not this module's data model, they are its storage.

The conversion outbox is registered read-only. An operator's question about
it is always "which uploads failed and what did Google say", never "let me
edit this row" — and a hand-edited outbox row is a conversion uploaded
twice or not at all.
"""
from django.contrib import admin

from .models import ConversionUpload, Funnel, UserAttribution


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


@admin.register(ConversionUpload)
class ConversionUploadAdmin(admin.ModelAdmin):
    list_display = ("click_id_type", "click_id", "status", "conversion_at",
                    "value", "currency", "attempts", "next_attempt_at",
                    "updated_at")
    list_filter = ("status", "click_id_type")
    search_fields = ("click_id", "conversion_action", "reason")
    ordering = ("-created_at",)
    readonly_fields = tuple(
        field.name for field in ConversionUpload._meta.fields
    )

    def has_add_permission(self, request):
        """A conversion is enqueued by the comm function, never typed in."""
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UserAttribution)
class UserAttributionAdmin(admin.ModelAdmin):
    """Read-only, and for one question: which click produced this account.

    Never editable. The row is evidence about a click that happened on
    somebody else's server; a hand-typed one is a conversion reported
    against a campaign that did not pay for it.
    """

    list_display = ("user_id", "click_id_type", "click_id", "clicked_at",
                    "captured_at", "source", "expired")
    list_filter = ("click_id_type", "source", "expired")
    search_fields = ("click_id", "user_id")
    ordering = ("-captured_at",)
    readonly_fields = tuple(
        field.name for field in UserAttribution._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


__all__ = ["ConversionUploadAdmin", "FunnelAdmin", "UserAttributionAdmin"]
