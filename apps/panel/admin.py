from django.contrib import admin

from .models import AuditLog, Job, Notification, Setting, SettingVersion


class SettingVersionInline(admin.TabularInline):
    model = SettingVersion
    extra = 0
    fields = ["version", "value", "changed_by", "changed_at", "change_note"]
    readonly_fields = fields
    can_delete = False


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ["key", "value", "current_version", "updated_at"]
    search_fields = ["key", "description"]
    readonly_fields = ["current_version"]
    inlines = [SettingVersionInline]

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["title", "notification_type", "level", "is_read", "created_at"]
    list_filter = ["notification_type", "level", "is_read"]


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ["__str__", "job_type", "status", "progress", "created_at", "finished_at"]
    list_filter = ["job_type", "status"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Только просмотр: журнал не редактируется."""

    list_display = ["timestamp", "username", "action", "content_type", "object_repr"]
    list_filter = ["action", "content_type"]
    search_fields = ["username", "object_repr", "note"]
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
