from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import CollectionRun, GoldenPage, SourceFeed, SourceHealthCheck, SourceIncident


@admin.register(SourceFeed)
class SourceFeedAdmin(AuditedModelAdmin):
    list_display = ["source", "rubric", "url", "kind", "is_active", "last_success_at", "last_error"]
    list_filter = ["kind", "is_active", "source__layer"]
    search_fields = ["url", "source__name"]


@admin.register(CollectionRun)
class CollectionRunAdmin(admin.ModelAdmin):
    """Только просмотр: запуски пишет сам сбор."""

    list_display = [
        "started_at", "source", "status", "found", "new", "versions",
        "duplicates", "skipped", "errors", "http_requests",
    ]  # fmt: skip
    list_filter = ["status", "source__layer", "source"]
    date_hierarchy = "started_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SourceHealthCheck)
class SourceHealthCheckAdmin(AuditedModelAdmin):
    list_display = ["source", "date", "check_type", "status"]
    list_filter = ["status", "check_type", "source__layer"]
    date_hierarchy = "date"


@admin.register(GoldenPage)
class GoldenPageAdmin(AuditedModelAdmin):
    list_display = ["url", "source", "is_active", "last_status", "last_checked_at"]
    list_filter = ["is_active", "last_status"]


@admin.register(SourceIncident)
class SourceIncidentAdmin(AuditedModelAdmin):
    list_display = ["source", "incident_type", "level", "started_at", "ended_at", "occurrences"]
    list_filter = ["level", "incident_type", "source__layer"]
