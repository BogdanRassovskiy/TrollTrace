from django.contrib import admin
from django.utils import timezone

from apps.panel.audit import AuditedModelAdmin

from .models import EventWindowResult, Institution, InstitutionalEvent


@admin.register(Institution)
class InstitutionAdmin(AuditedModelAdmin):
    list_display = ["name", "abbreviation", "group", "actor", "event_count"]
    list_filter = ["group"]
    search_fields = ["name", "abbreviation"]
    filter_horizontal = ["sources", "officials"]
    raw_id_fields = ["minister"]

    @admin.display(description="událostí")
    def event_count(self, obj):
        return obj.events.count()


@admin.register(InstitutionalEvent)
class InstitutionalEventAdmin(AuditedModelAdmin):
    """Ruční kódování událostí vrstvy I (README §2.1)."""

    list_display = [
        "date", "institution", "title", "event_type", "channel", "specificity", "narrative",
        "stance", "coded_by",
    ]  # fmt: skip
    list_filter = ["institution", "event_type", "channel", "specificity", "speaker_role", "stance"]
    search_fields = ["title", "summary", "speaker_name", "source_url"]
    date_hierarchy = "date"
    raw_id_fields = ["document", "speaker"]
    autocomplete_fields = ["narrative"]
    readonly_fields = ["narrative_version", "coded_by", "coded_at"]
    fieldsets = [
        (None, {"fields": ["institution", "date", "title", "summary", "source_url", "document"]}),
        (
            "Mluvčí a kanál",
            {"fields": ["speaker", "speaker_name", "speaker_role", "channel", "event_type"]},
        ),
        ("Narativ", {"fields": ["specificity", "narrative", "stance", "narrative_version"]}),
        ("Kódování", {"fields": ["notes", "coded_by", "coded_at"]}),
    ]

    def save_model(self, request, obj, form, change):
        if request.user.is_authenticated:
            obj.coded_by = request.user
        obj.coded_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(EventWindowResult)
class EventWindowResultAdmin(admin.ModelAdmin):
    list_display = ["event", "narrative", "layer", "days_before", "days_after", "settings_version"]
    list_filter = ["layer", "settings_version"]
    raw_id_fields = ["event"]
    filter_horizontal = ["control_narratives"]
