from django.contrib import admin

from apps.panel.audit import AuditedModelAdmin

from .models import Actor, ActorAccount, ActorInclusionPeriod, Source


@admin.register(Source)
class SourceAdmin(AuditedModelAdmin):
    list_display = ["name", "layer", "platform", "url", "is_active", "attribution_by"]
    list_filter = ["layer", "platform", "is_active"]
    search_fields = ["name", "url", "feed_url"]


class ActorInclusionPeriodInline(admin.TabularInline):
    model = ActorInclusionPeriod
    extra = 0


class ActorAccountInline(admin.TabularInline):
    model = ActorAccount
    extra = 0
    autocomplete_fields = ["source"]


@admin.register(Actor)
class ActorAdmin(AuditedModelAdmin):
    list_display = ["name", "actor_type", "layer", "party", "role", "is_hybrid", "status"]
    list_filter = ["layer", "actor_type", "status", "is_hybrid", "activity_cohort", "party"]
    search_fields = ["name", "party", "role", "accounts__account_id"]
    inlines = [ActorInclusionPeriodInline, ActorAccountInline]


@admin.register(ActorAccount)
class ActorAccountAdmin(AuditedModelAdmin):
    list_display = ["account_id", "actor", "source", "url"]
    list_filter = ["source__layer", "source__platform"]
    search_fields = ["account_id", "actor__name", "url"]
    autocomplete_fields = ["actor", "source"]
