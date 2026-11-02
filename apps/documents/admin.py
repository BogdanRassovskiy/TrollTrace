from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import Document, DocumentQuality, DocumentVersion, ReuseLink


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    fields = ["version", "title", "text_hash", "detected_at"]
    readonly_fields = fields
    can_delete = False


class DocumentQualityInline(admin.TabularInline):
    model = DocumentQuality
    extra = 0
    fields = ["score", "quality_rules_version", "is_current", "computed_at"]
    readonly_fields = fields
    can_delete = False


@admin.register(Document)
class DocumentAdmin(AuditedModelAdmin):
    list_display = ["__str__", "actor", "source", "published_at", "collected_at", "status"]
    list_filter = ["source__layer", "status", "is_relevant", "date_source"]
    search_fields = ["title", "url", "actor__name"]
    date_hierarchy = "collected_at"
    autocomplete_fields = ["actor", "source"]
    readonly_fields = ["url_normalized", "text_hash"]
    exclude = ["embedding", "minhash"]
    inlines = [DocumentVersionInline, DocumentQualityInline]


@admin.register(DocumentQuality)
class DocumentQualityAdmin(AuditedModelAdmin):
    list_display = ["document", "score", "quality_rules_version", "is_current", "computed_at"]
    list_filter = [
        "score",
        "is_current",
        "quality_rules_version",
        "blocked_page",
        "boilerplate_dup",
    ]
    raw_id_fields = ["document"]


@admin.register(ReuseLink)
class ReuseLinkAdmin(AuditedModelAdmin):
    list_display = [
        "source_document",
        "target_document",
        "link_type",
        "method",
        "similarity",
        "is_confirmed",
    ]
    list_filter = ["link_type", "method", "is_confirmed"]
    raw_id_fields = ["source_document", "target_document"]
