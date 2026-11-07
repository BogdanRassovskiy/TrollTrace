from django import forms
from django.contrib import admin, messages

from apps.panel.audit import AuditedModelAdmin

from .models import (
    Debunk,
    Label,
    LabelDecision,
    MarkerEvent,
    MarkerProposal,
    Narrative,
    NarrativeCandidate,
    NarrativeMarker,
    NarrativeVersion,
    NovelClaim,
    Screening,
    Topic,
)
from .retro import start_relabel


@admin.register(Topic)
class TopicAdmin(AuditedModelAdmin):
    list_display = ["code", "name", "keyword_list"]
    search_fields = ["code", "name"]

    @admin.display(description="klíčová slova")
    def keyword_list(self, obj):
        return ", ".join(obj.keywords)


class NarrativeVersionInline(admin.TabularInline):
    model = NarrativeVersion
    fk_name = "narrative"
    extra = 0
    fields = ["version", "name", "status", "changed_by", "changed_at", "change_note"]
    readonly_fields = fields
    can_delete = False


class DebunkInline(admin.TabularInline):
    model = Debunk
    extra = 0


@admin.register(Narrative)
class NarrativeAdmin(AuditedModelAdmin):
    list_display = [
        "code", "name", "level", "parent", "topic", "status", "discovered", "current_version",
    ]  # fmt: skip
    list_filter = ["status", "level", "discovered", "kind", "topic"]
    search_fields = ["code", "name", "definition"]
    autocomplete_fields = ["parent"]
    readonly_fields = ["current_version", "discovered", "added_at", "merged_into"]
    inlines = [NarrativeVersionInline, DebunkInline]
    actions = ["relabel_archive"]
    versioned = True

    @admin.action(description="Dohledat v archivu (zpětná anotace podle aktuální verze)")
    def relabel_archive(self, request, queryset):
        for narrative in queryset:
            start_relabel(narrative, request.user)
        self.message_user(
            request,
            f"Zpětné anotování zařazeno pro {queryset.count()} narativ(ů). Výsledek přijde "
            "jako upozornění.",
        )


@admin.register(Label)
class LabelAdmin(AuditedModelAdmin):
    list_display = ["document", "narrative_version", "stance", "origin", "confidence", "carrier"]
    list_filter = ["narrative", "stance", "origin", "document__source__layer"]
    raw_id_fields = ["document"]
    readonly_fields = ["details"]

    @admin.display(description="kdo narativ nese")
    def carrier(self, label):
        return (label.details or {}).get("carrier", "")


class NovelClaimInline(admin.TabularInline):
    model = NovelClaim
    extra = 0
    fields = ["document", "claim", "stance", "confidence", "carrier", "evidence", "similarity"]
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class NarrativeCandidateForm(forms.ModelForm):
    class Meta:
        model = NarrativeCandidate
        fields = ["description", "status", "decision_reason", "resulting_narrative"]
        help_texts = {
            "resulting_narrative": "U sloučení vyberte cílový narativ. U přijetí se doplní sám "
            "(narativ vzniklý ze shluku, případně se teď vytvoří).",
        }

    def clean(self):
        data = super().clean()
        merged = data.get("status") == NarrativeCandidate.Status.MERGED
        if merged and not data.get("resulting_narrative"):
            self.add_error("resulting_narrative", "Sloučení potřebuje cílový narativ.")
        return data


@admin.register(NarrativeCandidate)
class NarrativeCandidateAdmin(AuditedModelAdmin):
    """Shluky nových tvrzení. Změna stavu = rozhodnutí výzkumníka (přijmout, sloučit…)."""

    list_display = [
        "__str__", "status", "tracked", "n_documents", "n_authors", "claim_count",
        "window_start", "window_end",
    ]  # fmt: skip
    list_filter = ["status", ("tracked_since", admin.EmptyFieldListFilter)]
    search_fields = ["description", "claims__claim"]
    raw_id_fields = ["example_documents"]
    fields = [
        "description", "status", "decision_reason", "resulting_narrative", "tracked_since",
        "n_documents", "n_authors", "window_start", "window_end", "layers", "keywords",
        "nearest_narratives", "example_documents", "decided_by", "decided_at",
    ]  # fmt: skip
    readonly_fields = [
        "tracked_since", "n_documents", "n_authors", "window_start", "window_end", "layers",
        "keywords", "nearest_narratives", "decided_by", "decided_at",
    ]  # fmt: skip
    inlines = [NovelClaimInline]
    form = NarrativeCandidateForm

    @admin.display(description="sledován", boolean=True)
    def tracked(self, obj):
        return obj.tracked_since is not None

    @admin.display(description="tvrzení")
    def claim_count(self, obj):
        return obj.claims.count()

    def save_model(self, request, obj, form, change):
        from apps.processing import novelty

        previous = NarrativeCandidate.objects.filter(pk=obj.pk).first() if change else None
        new_status = obj.status
        if previous is None or previous.status == new_status:
            return super().save_model(request, obj, form, change)
        target = obj.resulting_narrative
        obj.status = previous.status
        obj.resulting_narrative = previous.resulting_narrative
        super().save_model(request, obj, form, change)
        novelty.decide(obj, new_status, request.user, target)
        messages.info(request, f"Rozhodnutí uloženo: {obj.get_status_display()}.")


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


@admin.register(Screening)
class ScreeningAdmin(admin.ModelAdmin):
    list_display = ["document", "n_claims", "model_name", "cost_usd", "checked_at", "error"]
    list_filter = ["model_name", ("checked_at", admin.EmptyFieldListFilter)]
    raw_id_fields = ["document"]
    readonly_fields = [field.name for field in Screening._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(NovelClaim)
class NovelClaimAdmin(admin.ModelAdmin):
    list_display = ["claim", "stance", "confidence", "candidate", "matched_narrative", "similarity"]
    list_filter = ["stance", "document__source__layer"]
    search_fields = ["claim", "evidence", "carrier"]
    raw_id_fields = ["document"]
    exclude = ["embedding"]
    readonly_fields = [f.name for f in NovelClaim._meta.fields if f.name != "embedding"]

    def has_add_permission(self, request):
        return False


class MarkerEventInline(admin.TabularInline):
    model = MarkerEvent
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in MarkerEvent._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(NarrativeMarker)
class NarrativeMarkerAdmin(admin.ModelAdmin):
    """Stav markeru mění pravidla a tlačítka v panelu (s historií), ne ruční úprava."""

    list_display = [
        "phrase", "narrative", "status", "n_proposals", "n_authors", "n_checks", "precision",
        "status_changed_at",
    ]  # fmt: skip
    list_filter = ["status", "narrative__topic"]
    search_fields = ["phrase", "key", "narrative__code", "narrative__name"]
    readonly_fields = [field.name for field in NarrativeMarker._meta.fields]
    inlines = [MarkerEventInline]

    def has_add_permission(self, request):
        return False


@admin.register(MarkerProposal)
class MarkerProposalAdmin(admin.ModelAdmin):
    list_display = ["phrase", "marker", "narrative", "document", "created_at"]
    search_fields = ["phrase", "marker__key"]
    raw_id_fields = ["document", "decision"]
    readonly_fields = [field.name for field in MarkerProposal._meta.fields]

    def has_add_permission(self, request):
        return False
