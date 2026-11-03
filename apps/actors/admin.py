from django.contrib import admin
from django.templatetags.static import static
from django.utils.html import format_html

from apps.ingestion.models import SourceFeed
from apps.panel.audit import AuditedModelAdmin

from .models import Actor, ActorAccount, ActorInclusionPeriod, ActorPosition, Source


class SourceFeedInline(admin.TabularInline):
    model = SourceFeed
    extra = 0
    fields = [
        "url",
        "kind",
        "rubric",
        "is_active",
        "last_success_at",
        "last_item_count",
        "last_error",
    ]
    readonly_fields = ["last_success_at", "last_item_count", "last_error"]


@admin.register(Source)
class SourceAdmin(AuditedModelAdmin):
    list_display = ["name", "layer", "platform", "url", "is_active", "collect_interval_minutes"]
    list_filter = ["layer", "platform", "is_active"]
    search_fields = ["name", "url", "feeds__url"]
    inlines = [SourceFeedInline]


def photo_icon(actor, size=32):
    # media/ не хранится в git: без файла показываем заглушку из static.
    url = actor.photo.url if actor.photo else static("img/actor-placeholder.svg")
    return format_html(
        '<img src="{}" width="{}" height="{}" alt="" style="border-radius:50%;object-fit:cover">',
        url,
        size,
        size,
    )


class ActorInclusionPeriodInline(admin.TabularInline):
    model = ActorInclusionPeriod
    extra = 0


class ActorPositionInline(admin.TabularInline):
    model = ActorPosition
    extra = 0
    fields = ["kind", "title", "organization", "start_date", "end_date", "source_url"]


class ActorAccountInline(admin.TabularInline):
    model = ActorAccount
    extra = 0
    autocomplete_fields = ["source"]


class PublicOfficeFilter(admin.SimpleListFilter):
    title = "veřejná funkce"
    parameter_name = "public_office"

    def lookups(self, request, model_admin):
        return [("yes", "Zastává"), ("no", "Nezastává (jen stranická nebo žádná)")]

    def queryset(self, request, queryset):
        holders = ActorPosition.objects.filter(
            end_date__isnull=True, kind__in=ActorPosition.PUBLIC_OFFICE_KINDS
        ).values("actor_id")
        if self.value() == "yes":
            return queryset.filter(pk__in=holders)
        if self.value() == "no":
            return queryset.exclude(pk__in=holders)
        return queryset


@admin.register(Actor)
class ActorAdmin(AuditedModelAdmin):
    list_display = [
        "icon", "name", "actor_type", "layer", "party", "role", "public_office", "is_hybrid",
        "status",
    ]  # fmt: skip
    list_display_links = ["icon", "name"]
    readonly_fields = ["photo_preview"]
    list_filter = [
        "layer", "actor_type", "status", PublicOfficeFilter, "manual_inclusion", "is_hybrid",
        "security_portfolio", "activity_cohort", "party",
    ]  # fmt: skip
    search_fields = ["name", "party", "role", "positions__title", "accounts__account_id"]
    inlines = [ActorPositionInline, ActorInclusionPeriodInline, ActorAccountInline]

    @admin.display(description="veřejná funkce", boolean=True)
    def public_office(self, actor):
        return actor.holds_public_office

    @admin.display(description="")
    def icon(self, actor):
        return photo_icon(actor)

    @admin.display(description="ikona")
    def photo_preview(self, actor):
        return photo_icon(actor, 64)


@admin.register(ActorAccount)
class ActorAccountAdmin(AuditedModelAdmin):
    list_display = ["account_id", "actor", "source", "url"]
    list_filter = ["source__layer", "source__platform"]
    search_fields = ["account_id", "actor__name", "url"]
    autocomplete_fields = ["actor", "source"]


@admin.register(ActorPosition)
class ActorPositionAdmin(AuditedModelAdmin):
    list_display = ["actor", "title", "kind", "organization", "start_date", "end_date"]
    list_filter = ["kind", "end_date"]
    search_fields = ["actor__name", "title", "organization"]
    autocomplete_fields = ["actor"]
