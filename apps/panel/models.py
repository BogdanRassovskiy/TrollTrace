"""Панель исследователя: настройки, уведомления, фоновые задачи, журнал аудита (README §7.2)."""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q

from apps.core.models import VersionedModel, VersionRecord


class Setting(VersionedModel):
    """Настройка исследования (пороги, окна, модели). Изменения версионируются."""

    VERSIONED_FIELDS = ("value",)

    key = models.SlugField("ключ", max_length=100, unique=True)
    value = models.JSONField("значение")
    description = models.TextField("описание", blank=True)
    updated_at = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        verbose_name = "настройка"
        verbose_name_plural = "настройки"
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} = {self.value!r}"

    def create_version_record(self, snapshot, *, changed_by, change_note):
        SettingVersion.objects.create(
            setting=self,
            version=self.current_version,
            changed_by=changed_by,
            change_note=change_note,
            **snapshot,
        )


class SettingVersion(VersionRecord):
    setting = models.ForeignKey(
        Setting, verbose_name="настройка", on_delete=models.CASCADE, related_name="versions"
    )
    value = models.JSONField("значение")

    class Meta(VersionRecord.Meta):
        verbose_name = "версия настройки"
        verbose_name_plural = "история настроек"
        constraints = [
            models.UniqueConstraint(
                fields=["setting", "version"], name="setting_version_unique_number"
            ),
        ]

    def __str__(self):
        return f"{self.setting.key} v{self.version}"


class Notification(models.Model):
    """Уведомление для исследователя: кандидаты, всплески, ошибки сбора, завершение задач."""

    class Type(models.TextChoices):
        NARRATIVE_CANDIDATE = "candidate", "Новый кандидат в нарративы"
        SPIKE = "spike", "Всплеск у актора"
        COLLECTION = "collection", "Сбор данных"
        JOB = "job", "Фоновая задача"
        OTHER = "other", "Другое"

    class Level(models.TextChoices):
        INFO = "info", "Информация"
        WARNING = "warning", "Предупреждение"
        CRITICAL = "critical", "Критично"

    notification_type = models.CharField("тип", max_length=12, choices=Type.choices)
    level = models.CharField("уровень", max_length=10, choices=Level.choices, default=Level.INFO)
    title = models.CharField("заголовок", max_length=300)
    text = models.TextField("текст", blank=True)
    url = models.CharField("ссылка в панели", max_length=500, blank=True)

    content_type = models.ForeignKey(
        ContentType, verbose_name="тип объекта", on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.CharField("ID объекта", max_length=64, blank=True)
    target = GenericForeignKey("content_type", "object_id")

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Пусто — всем исследователям.",
    )
    is_read = models.BooleanField("прочитано", default=False)
    read_at = models.DateTimeField("когда прочитано", null=True, blank=True)
    sent_to_telegram = models.BooleanField("отправлено в Telegram", default=False)
    created_at = models.DateTimeField("создано", auto_now_add=True)

    class Meta:
        verbose_name = "уведомление"
        verbose_name_plural = "уведомления"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_read", "-created_at"])]

    def __str__(self):
        return self.title


class Job(models.Model):
    """Фоновая задача (сбор, ретроразметка, пересчёт агрегатов) и её статус."""

    class Type(models.TextChoices):
        COLLECT = "collect", "Сбор"
        RETRO_COLLECT = "retro_collect", "Ретроспективный сбор"
        EMBED = "embed", "Эмбеддинги"
        RELABEL = "relabel", "Ретроразметка"
        AGGREGATE = "aggregate", "Пересчёт агрегатов"
        QUALITY = "quality", "Пересчёт оценок качества"
        OTHER = "other", "Другое"

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        RUNNING = "running", "Выполняется"
        SUCCEEDED = "succeeded", "Готово"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменено"

    job_type = models.CharField("тип", max_length=15, choices=Type.choices)
    status = models.CharField(
        "статус", max_length=10, choices=Status.choices, default=Status.PENDING
    )
    celery_task_id = models.CharField("ID задачи Celery", max_length=64, blank=True)
    params = models.JSONField("параметры", default=dict, blank=True)
    progress = models.FloatField("прогресс", default=0)
    message = models.TextField("сообщение", blank=True)
    result = models.JSONField("результат", default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто запустил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("создано", auto_now_add=True)
    started_at = models.DateTimeField("начато", null=True, blank=True)
    finished_at = models.DateTimeField("завершено", null=True, blank=True)

    class Meta:
        verbose_name = "фоновая задача"
        verbose_name_plural = "фоновые задачи"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(progress__gte=0, progress__lte=1), name="job_progress_0_1"
            ),
        ]

    def __str__(self):
        return f"{self.get_job_type_display()} #{self.pk} ({self.status})"


class AuditLog(models.Model):
    """Журнал действий в панели: кто, когда, что изменил, старое → новое.

    Только добавление: запись нельзя изменить или удалить через ORM. Запись в журнал из
    панели (сервис аудита) — Этап 2.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Создание"
        UPDATE = "update", "Изменение"
        ARCHIVE = "archive", "Архивация"
        RESTORE = "restore", "Восстановление"
        DELETE = "delete", "Удаление"
        DECISION = "decision", "Решение по кандидату"
        RUN = "run", "Запуск задачи"
        LOGIN = "login", "Вход"

    timestamp = models.DateTimeField("когда", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    username = models.CharField(
        "имя пользователя", max_length=150, blank=True, help_text="Сохраняется на случай удаления."
    )
    action = models.CharField("действие", max_length=10, choices=Action.choices)
    content_type = models.ForeignKey(
        ContentType, verbose_name="тип объекта", on_delete=models.PROTECT, null=True, blank=True
    )
    object_id = models.CharField("ID объекта", max_length=64, blank=True)
    object_repr = models.CharField("объект", max_length=300, blank=True)
    changes = models.JSONField(
        "изменения", default=dict, blank=True, help_text='{"поле": [старое, новое]}'
    )
    note = models.TextField("комментарий", blank=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)

    class Meta:
        verbose_name = "запись журнала аудита"
        verbose_name_plural = "журнал аудита"
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.username} {self.action} {self.object_repr}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Журнал аудита только дополняется.")
        if self.user is not None and not self.username:
            self.username = self.user.get_username()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Журнал аудита только дополняется.")
