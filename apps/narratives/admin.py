from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import (
    Debunk,
    Label,
    LabelDecision,
    Narrative,
    NarrativeCandidate,
    NarrativeVersion,
    Topic,
)


@admin.register(Topic)
class TopicAdmin(AuditedModelAdmin):
    list_display = ["code", "name", "keyword_list"]
    search_fields = ["code", "name"]

    @admin.display(description="klíčová slova")
    def keyword_list(self, obj):
        return ", ".join(obj.keywords)


class NarrativeVersionInline(admin.TabularInline):
    model = NarrativeVersion
    extra = 0
    fields = ["version", "name", "status", "changed_by", "changed_at", "change_note"]
    readonly_fields = fields
    can_delete = False


class DebunkInline(admin.TabularInline):
    model = Debunk
    extra = 0


@admin.register(Narrative)
class NarrativeAdmin(AuditedModelAdmin):
    list_display = ["code", "name", "kind", "topic", "status", "current_version", "added_at"]
    list_filter = ["status", "kind", "topic"]
    search_fields = ["code", "name", "definition"]
    readonly_fields = ["current_version", "added_at"]
    inlines = [NarrativeVersionInline, DebunkInline]
    versioned = True


@admin.register(Label)
class LabelAdmin(AuditedModelAdmin):
    list_display = ["document", "narrative_version", "stance", "origin", "confidence", "carrier"]
    list_filter = ["narrative", "stance", "origin", "document__source__layer"]
    raw_id_fields = ["document"]
    readonly_fields = ["details"]

    @admin.display(description="kdo narativ nese")
    def carrier(self, label):
        return (label.details or {}).get("carrier", "")


@admin.register(NarrativeCandidate)
class NarrativeCandidateAdmin(AuditedModelAdmin):
    list_display = ["__str__", "status", "n_documents", "n_authors", "window_start", "window_end"]
    list_filter = ["status"]
    raw_id_fields = ["example_documents"]


@admin.register(LabelDecision)
class LabelDecisionAdmin(admin.ModelAdmin):
    """Rozhodnutí modelu jsou doklad (i záporná): needitují se, ruční štítek se dává zvlášť."""

    list_display = [
        "document", "narrative", "present", "stance", "confidence", "similarity", "carrier",
        "model_name", "decided_at",
    ]  # fmt: skip
    list_filter = ["narrative", "present", "stance", "model_name", "document__source__layer"]
    search_fields = ["document__title", "evidence", "reason", "carrier"]
    raw_id_fields = ["document"]
    readonly_fields = [field.name for field in LabelDecision._meta.fields]

    def has_add_permission(self, request):
        return False
