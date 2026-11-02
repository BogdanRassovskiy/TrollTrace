"""Базовые абстрактные модели, общие для всех приложений."""

from django.conf import settings
from django.db import models, transaction


class TimeStampedModel(models.Model):
    """Время создания и последнего изменения записи."""

    created_at = models.DateTimeField("создано", auto_now_add=True)
    updated_at = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        abstract = True


class VersionedModel(models.Model):
    """Модель с обязательной историей изменений (кодбук, настройки исследования).

    Любое сохранение, меняющее поля из VERSIONED_FIELDS, увеличивает current_version и в той же
    транзакции пишет снимок в модель истории. Строка блокируется (SELECT ... FOR UPDATE), а
    уникальность «объект × номер версии» в модели истории не даёт выдать номер дважды.
    """

    VERSIONED_FIELDS: tuple[str, ...] = ()

    current_version = models.PositiveIntegerField("текущая версия", default=0, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, changed_by=None, change_note: str = "", **kwargs):
        with transaction.atomic():
            stored = None
            if not self._state.adding and self.pk is not None:
                stored = (
                    type(self)
                    .objects.select_for_update()
                    .filter(pk=self.pk)
                    .values("current_version", *self._versioned_attnames())
                    .first()
                )
            snapshot = self.versioned_snapshot()
            stored_version = stored.pop("current_version") if stored else 0
            changed = stored != snapshot
            if changed:
                self.current_version = stored_version + 1
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = {*kwargs["update_fields"], "current_version"}
            super().save(*args, **kwargs)
            if changed:
                self.create_version_record(snapshot, changed_by=changed_by, change_note=change_note)

    def _versioned_attnames(self) -> list[str]:
        return [self._meta.get_field(name).attname for name in self.VERSIONED_FIELDS]

    def versioned_snapshot(self) -> dict:
        """Значения версионируемых полей; FK — как *_id, чтобы снимок можно было сравнить с БД."""
        return {attname: getattr(self, attname) for attname in self._versioned_attnames()}

    def create_version_record(self, snapshot: dict, *, changed_by, change_note: str):
        """Создаёт запись истории для версии self.current_version из снимка полей."""
        raise NotImplementedError


class VersionRecord(models.Model):
    """Общие поля записи истории версий: кто, когда и зачем изменил."""

    version = models.PositiveIntegerField("версия")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто изменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField("когда", auto_now_add=True)
    change_note = models.TextField("комментарий к изменению", blank=True)

    class Meta:
        abstract = True
        ordering = ["-version"]
