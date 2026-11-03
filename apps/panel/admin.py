from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from . import audit
from .audit import AuditedModelAdmin
from .models import AuditLog, Job, Notification, Setting, SettingVersion


class SettingVersionInline(admin.TabularInline):
    model = SettingVersion
    extra = 0
    fields = ["version", "value", "changed_by", "changed_at", "change_note"]
    readonly_fields = fields
    can_delete = False


@admin.register(Setting)
class SettingAdmin(AuditedModelAdmin):
    list_display = ["key", "value", "current_version", "updated_at"]
    search_fields = ["key", "description"]
    readonly_fields = ["current_version"]
    inlines = [SettingVersionInline]
    versioned = True


@admin.register(Notification)
class NotificationAdmin(AuditedModelAdmin):
    list_display = ["title", "notification_type", "level", "is_read", "created_at"]
    list_filter = ["notification_type", "level", "is_read"]


@admin.register(Job)
class JobAdmin(AuditedModelAdmin):
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


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class AuditedUserAdmin(AuditedModelAdmin, UserAdmin):
    """Пользователи: смена ролей (групп) и пароля тоже попадает в журнал."""

    @staticmethod
    def _groups(user) -> list[str]:
        if user.pk is None:
            return []
        return sorted(User.objects.get(pk=user.pk).groups.values_list("name", flat=True))

    def save_model(self, request, obj, form, change):
        obj._audit_groups_before = self._groups(obj)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        before, after = (
            getattr(form.instance, "_audit_groups_before", []),
            self._groups(form.instance),
        )
        if before != after:
            audit.log(
                request, AuditLog.Action.ROLE, form.instance, changes={"groups": [before, after]}
            )

    def user_change_password(self, request, id, form_url=""):
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and response.status_code == 302:
            audit.log(
                request, AuditLog.Action.UPDATE, self.get_object(request, id), note="Heslo změněno"
            )
        return response
