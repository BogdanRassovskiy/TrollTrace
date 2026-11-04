"""Валидация в администрации: просмотр. Выборки создаются и решаются в панели."""

from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import Adjudication, Annotation, Report, Review, Sample, SampleItem


class ReadOnlyAdmin(AuditedModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Sample)
class SampleAdmin(ReadOnlyAdmin):
    list_display = ["name", "kind", "status", "created_at", "created_by"]
    list_filter = ["kind", "status"]


@admin.register(SampleItem)
class SampleItemAdmin(ReadOnlyAdmin):
    list_display = ["sample", "position", "stratum", "weight", "document_id"]
    list_filter = ["sample", "stratum"]


class AnnotationInline(admin.TabularInline):
    model = Annotation
    extra = 0
    can_delete = False
    readonly_fields = ["narrative_version", "value"]


@admin.register(Review)
class ReviewAdmin(ReadOnlyAdmin):
    list_display = ["item", "annotator", "unclear", "reuse_verdict", "updated_at"]
    list_filter = ["item__sample", "annotator", "unclear"]
    inlines = [AnnotationInline]


@admin.register(Adjudication)
class AdjudicationAdmin(ReadOnlyAdmin):
    list_display = ["item", "narrative_version", "value", "decided_by", "decided_at"]


@admin.register(Report)
class ReportAdmin(ReadOnlyAdmin):
    list_display = ["sample", "model_name", "created_by", "created_at"]
