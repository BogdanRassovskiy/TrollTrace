from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import GoldenPage, SourceHealthCheck, SourceIncident


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
