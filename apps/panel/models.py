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

    key = models.SlugField("klíč", max_length=100, unique=True)
    value = models.JSONField("hodnota")
    description = models.TextField("popis", blank=True)
    updated_at = models.DateTimeField("změněno", auto_now=True)

    class Meta:
        verbose_name = "nastavení"
        verbose_name_plural = "nastavení"
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
        Setting, verbose_name="nastavení", on_delete=models.CASCADE, related_name="versions"
    )
    value = models.JSONField("hodnota")

    class Meta(VersionRecord.Meta):
        verbose_name = "verze nastavení"
        verbose_name_plural = "historie nastavení"
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
        NARRATIVE_CANDIDATE = "candidate", "Nový kandidát na narativ"
        SPIKE = "spike", "Výkyv u aktéra"
        COLLECTION = "collection", "Sběr dat"
        JOB = "job", "Úloha na pozadí"
        OTHER = "other", "Jiné"

    class Level(models.TextChoices):
        INFO = "info", "Informace"
        WARNING = "warning", "Varování"
        CRITICAL = "critical", "Kritické"

    notification_type = models.CharField("typ", max_length=12, choices=Type.choices)
    level = models.CharField("úroveň", max_length=10, choices=Level.choices, default=Level.INFO)
    title = models.CharField("titulek", max_length=300)
    text = models.TextField("text", blank=True)
    url = models.CharField("odkaz v panelu", max_length=500, blank=True)

    content_type = models.ForeignKey(
        ContentType, verbose_name="typ objektu", on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.CharField("ID objektu", max_length=64, blank=True)
    target = GenericForeignKey("content_type", "object_id")

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="příjemce",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Prázdné — všem výzkumníkům.",
    )
    is_read = models.BooleanField("přečteno", default=False)
    read_at = models.DateTimeField("kdy přečteno", null=True, blank=True)
    sent_to_telegram = models.BooleanField("odesláno do Telegramu", default=False)
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)

    class Meta:
        verbose_name = "upozornění"
        verbose_name_plural = "upozornění"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_read", "-created_at"])]

    def __str__(self):
        return self.title


class Job(models.Model):
    """Фоновая задача (сбор, ретроразметка, пересчёт агрегатов) и её статус."""

    class Type(models.TextChoices):
        COLLECT = "collect", "Sběr"
        RETRO_COLLECT = "retro_collect", "Retrospektivní sběr"
        EMBED = "embed", "Embeddingy"
        RELABEL = "relabel", "Zpětné anotování"
        AGGREGATE = "aggregate", "Přepočet agregátů"
        QUALITY = "quality", "Přepočet hodnocení kvality"
        OTHER = "other", "Jiné"

    class Status(models.TextChoices):
        PENDING = "pending", "Ve frontě"
        RUNNING = "running", "Probíhá"
        SUCCEEDED = "succeeded", "Hotovo"
        FAILED = "failed", "Chyba"
        CANCELLED = "cancelled", "Zrušeno"

    job_type = models.CharField("typ", max_length=15, choices=Type.choices)
    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.PENDING)
    celery_task_id = models.CharField("ID úlohy Celery", max_length=64, blank=True)
    params = models.JSONField("parametry", default=dict, blank=True)
    progress = models.FloatField("průběh", default=0)
    message = models.TextField("zpráva", blank=True)
    result = models.JSONField("výsledek", default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="kdo spustil",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)
    started_at = models.DateTimeField("zahájeno", null=True, blank=True)
    finished_at = models.DateTimeField("dokončeno", null=True, blank=True)

    class Meta:
        verbose_name = "úloha na pozadí"
        verbose_name_plural = "úlohy na pozadí"
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

    Только добавление: запись нельзя изменить или удалить ни через ORM, ни SQL-запросом
    (триггер в миграции panel/0002). Пишется через сервис apps.panel.audit.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Vytvoření"
        UPDATE = "update", "Změna"
        ARCHIVE = "archive", "Archivace"
        RESTORE = "restore", "Obnovení"
        DELETE = "delete", "Smazání"
        DECISION = "decision", "Rozhodnutí o kandidátovi"
        RUN = "run", "Spuštění úlohy"
        ROLE = "role", "Změna role"
        LOGIN = "login", "Přihlášení"
        LOGOUT = "logout", "Odhlášení"
        LOGIN_FAILED = "login_failed", "Neúspěšné přihlášení"

    timestamp = models.DateTimeField("kdy", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="uživatel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    username = models.CharField(
        "uživatelské jméno", max_length=150, blank=True, help_text="Uchovává se pro případ smazání."
    )
    action = models.CharField("akce", max_length=12, choices=Action.choices)
    content_type = models.ForeignKey(
        ContentType, verbose_name="typ objektu", on_delete=models.PROTECT, null=True, blank=True
    )
    object_id = models.CharField("ID objektu", max_length=64, blank=True)
    object_repr = models.CharField("objekt", max_length=300, blank=True)
    changes = models.JSONField(
        "změny", default=dict, blank=True, help_text='{"pole": [staré, nové]}'
    )
    note = models.TextField("komentář", blank=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)

    class Meta:
        verbose_name = "záznam auditního protokolu"
        verbose_name_plural = "auditní protokol"
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.username} {self.action} {self.object_repr}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Auditní protokol lze pouze doplňovat.")
        if self.user is not None and not self.username:
            self.username = self.user.get_username()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Auditní protokol lze pouze doplňovat.")
