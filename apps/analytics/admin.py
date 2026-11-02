from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import WeeklyAggregate


@admin.register(WeeklyAggregate)
class WeeklyAggregateAdmin(AuditedModelAdmin):
    list_display = [
        "week",
        "actor",
        "group_key",
        "narrative",
        "n_documents",
        "n_total",
        "share",
        "z_score",
    ]
    list_filter = ["narrative", "min_quality"]
    search_fields = ["actor__name", "group_key"]
    date_hierarchy = "week"
    raw_id_fields = ["actor"]
