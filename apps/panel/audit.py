"""Сервис журнала аудита (README §7.2): кто, когда, что изменил, старое и новое значение.

Использование в представлениях панели:

    before = audit.snapshot(obj)
    ...изменить и сохранить obj...
    audit.log_change(request, obj, before)

или через AuditedFormMixin для CreateView / UpdateView и AuditedModelAdmin для админки.
"""

import json

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from pgvector.django import VectorField

from .models import AuditLog

# Поля, которые не пишутся в журнал: бинарные и эмбеддинги (не несут смысла), автометки
# времени, а также хэш пароля и время входа пользователя (секрет и шум).
SKIP_FIELD_TYPES = (models.BinaryField, VectorField)
SKIP_FIELD_NAMES = {"created_at", "updated_at", "password", "last_login"}


def _jsonable(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def snapshot(instance) -> dict:
    """Значения конкретных полей модели; FK — как id. Пустой dict для несохранённого объекта."""
    if instance is None or instance.pk is None:
        return {}
    data = {}
    for field in instance._meta.concrete_fields:
        if isinstance(field, SKIP_FIELD_TYPES) or field.name in SKIP_FIELD_NAMES:
            continue
        data[field.name] = _jsonable(field.value_from_object(instance))
    return data


def stored_snapshot(instance) -> dict:
    """Снимок того, что сейчас лежит в БД (до сохранения изменений из формы)."""
    if instance.pk is None:
        return {}
    stored = type(instance)._default_manager.filter(pk=instance.pk).first()
    return snapshot(stored)


def diff(before: dict, after: dict) -> dict:
    """{поле: [старое, новое]} только по изменившимся полям."""
    keys = before.keys() | after.keys()
    return {
        key: [before.get(key), after.get(key)]
        for key in sorted(keys)
        if before.get(key) != after.get(key)
    }


def _client_ip(request) -> str | None:
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def log(
    request,
    action: str,
    instance=None,
    *,
    changes: dict | None = None,
    note: str = "",
    user=None,
    username: str = "",
) -> AuditLog:
    """Пишет запись журнала. user берётся из request, если не передан явно (CLI, задачи)."""
    if user is None and request is not None and request.user.is_authenticated:
        user = request.user
    entry = AuditLog(
        user=user,
        username=user.get_username() if user is not None else username[:150],
        action=action,
        changes=changes or {},
        note=note,
        ip_address=_client_ip(request),
    )
    if instance is not None:
        entry.content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
        entry.object_id = str(instance.pk)
        entry.object_repr = str(instance)[:300]
    entry.save()
    return entry


def log_create(request, instance, **kwargs) -> AuditLog:
    return log(
        request, AuditLog.Action.CREATE, instance, changes=diff({}, snapshot(instance)), **kwargs
    )


def log_change(request, instance, before: dict, **kwargs) -> AuditLog | None:
    """Пишет изменение; если по существу ничего не изменилось — не пишет ничего."""
    changes = diff(before, snapshot(instance))
    if not changes:
        return None
    return log(request, AuditLog.Action.UPDATE, instance, changes=changes, **kwargs)


def log_delete(request, instance, before: dict | None = None, **kwargs) -> AuditLog:
    """Вызывать до удаления (нужен pk) или передать снимок, снятый до удаления."""
    before = before if before is not None else snapshot(instance)
    return log(request, AuditLog.Action.DELETE, instance, changes=diff(before, {}), **kwargs)


class AuditedFormMixin:
    """Для CreateView / UpdateView панели: сохраняет форму и пишет запись журнала."""

    def form_valid(self, form):
        instance = form.instance
        before = stored_snapshot(instance)
        response = super().form_valid(form)
        if before:
            log_change(self.request, self.object, before)
        else:
            log_create(self.request, self.object)
        return response


class AuditedModelAdmin(admin.ModelAdmin):
    """Админка с журналом аудита: создание, изменение, удаление, в том числе через инлайны.

    Запись журнала и само изменение идут в одной транзакции (представления админки атомарны):
    если сохранение упадёт, записи в журнале не останется.
    """

    #: Для VersionedModel: передать автора изменения в историю версий.
    versioned = False

    def save_model(self, request, obj, form, change):
        before = stored_snapshot(obj) if change else {}
        if self.versioned:
            obj.save(changed_by=request.user)
        else:
            super().save_model(request, obj, form, change)
        if change:
            log_change(request, obj, before)
        else:
            log_create(request, obj)

    def delete_model(self, request, obj):
        log_delete(request, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            log_delete(request, obj)
        super().delete_queryset(request, queryset)

    def save_formset(self, request, form, formset, change):
        before = {
            f.instance.pk: stored_snapshot(f.instance)
            for f in formset.initial_forms
            if f.instance.pk is not None and f.has_changed()
        }
        for deleted in formset.deleted_forms:
            if deleted.instance.pk is not None:
                log_delete(request, deleted.instance, before.get(deleted.instance.pk))
        super().save_formset(request, form, formset, change)
        for obj in formset.new_objects:
            log_create(request, obj)
        for obj, _fields in formset.changed_objects:
            log_change(request, obj, before.get(obj.pk, {}))
