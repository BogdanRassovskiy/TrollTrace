from django.contrib import admin

from .models import Debunk, Label, Narrative, NarrativeCandidate, NarrativeVersion, Topic


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ["code", "name"]
    search_fields = ["code", "name"]


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
class NarrativeAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "topic", "status", "current_version", "added_at"]
    list_filter = ["status", "topic"]
    search_fields = ["code", "name", "definition"]
    readonly_fields = ["current_version", "added_at"]
    inlines = [NarrativeVersionInline, DebunkInline]

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    list_display = ["document", "narrative_version", "stance", "origin", "confidence"]
    list_filter = ["narrative", "stance", "origin"]
    raw_id_fields = ["document"]


@admin.register(NarrativeCandidate)
class NarrativeCandidateAdmin(admin.ModelAdmin):
    list_display = ["__str__", "status", "n_documents", "n_authors", "window_start", "window_end"]
    list_filter = ["status"]
    raw_id_fields = ["example_documents"]
