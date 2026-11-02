"""Мониторинг сбора: проверки здоровья, контрольные страницы, инциденты (README §4.2)."""

from django.db import models
from django.db.models import F, Q

from apps.actors.models import Source


class Level(models.TextChoices):
    OK = "ok", "Норма"
    WARNING = "warning", "Предупреждение"
    CRITICAL = "critical", "Критично"


class SourceFeed(models.Model):
    """RSS/Atom-лента или sitemap источника. У медиа — по ленте на политическую рубрику."""

    class Kind(models.TextChoices):
        RSS = "rss", "RSS / Atom"
        SITEMAP = "sitemap", "Sitemap"

    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.CASCADE, related_name="feeds"
    )
    url = models.URLField("адрес ленты", max_length=1000)
    kind = models.CharField("тип", max_length=10, choices=Kind.choices, default=Kind.RSS)
    rubric = models.CharField("рубрика", max_length=200, blank=True)
    is_active = models.BooleanField("используется", default=True)

    # Условные запросы (304 Not Modified) и состояние для страницы «Источники».
    etag = models.CharField(max_length=300, blank=True, editable=False)
    last_modified = models.CharField(max_length=100, blank=True, editable=False)
    last_fetched_at = models.DateTimeField("последний запрос", null=True, blank=True)
    last_success_at = models.DateTimeField("последняя успешная загрузка", null=True, blank=True)
    last_item_count = models.PositiveIntegerField("записей в ленте", null=True, blank=True)
    last_error = models.TextField("последняя ошибка", blank=True)

    class Meta:
        verbose_name = "лента источника"
        verbose_name_plural = "ленты источников"
        constraints = [
            models.UniqueConstraint(fields=["source", "url"], name="source_feed_unique_url"),
        ]

    def __str__(self):
        return f"{self.source.name}: {self.rubric or self.url}"


class CollectionRun(models.Model):
    """Один запуск сбора по источнику: что найдено и сохранено. Основа мониторинга (§4.2)."""

    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        OK = "ok", "Успешно"
        PARTIAL = "partial", "С ошибками"
        FAILED = "failed", "Ошибка"

    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.CASCADE, related_name="collection_runs"
    )
    started_at = models.DateTimeField("начало", auto_now_add=True)
    finished_at = models.DateTimeField("конец", null=True, blank=True)
    status = models.CharField(
        "статус", max_length=10, choices=Status.choices, default=Status.RUNNING
    )
    found = models.PositiveIntegerField("найдено", default=0)
    new = models.PositiveIntegerField("новых", default=0)
    versions = models.PositiveIntegerField("новых версий", default=0)
    unchanged = models.PositiveIntegerField("без изменений", default=0)
    duplicates = models.PositiveIntegerField("дублей по тексту", default=0)
    skipped = models.PositiveIntegerField("пропущено", default=0)
    errors = models.PositiveIntegerField("ошибок", default=0)
    http_requests = models.PositiveIntegerField("HTTP-запросов", default=0)
    details = models.JSONField(
        "подробности", default=dict, blank=True, help_text="Примеры ошибок, статусы HTTP."
    )

    class Meta:
        verbose_name = "запуск сбора"
        verbose_name_plural = "запуски сбора"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "-started_at"])]

    def __str__(self):
        return f"{self.source.name} {self.started_at:%Y-%m-%d %H:%M}: {self.status}"


class SourceHealthCheck(models.Model):
    """Результат одной проверки мониторинга по источнику за день."""

    class CheckType(models.TextChoices):
        VOLUME = "volume", "Объём"
        EXTRACTION = "extraction", "Качество извлечения"
        GOLDEN = "golden", "Контрольные страницы"
        SELECTORS = "selectors", "Структурный отпечаток"
        HTTP = "http", "HTTP / RSS / доступность"
        QUALITY = "quality", "Средняя оценка качества"

    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.CASCADE, related_name="health_checks"
    )
    date = models.DateField("день")
    check_type = models.CharField("проверка", max_length=12, choices=CheckType.choices)
    status = models.CharField("результат", max_length=10, choices=Level.choices)
    metrics = models.JSONField(
        "метрики", default=dict, blank=True, help_text="Например: n, медиана, доля без даты."
    )
    details = models.TextField("подробности", blank=True)
    checked_at = models.DateTimeField("проверено", auto_now=True)

    class Meta:
        verbose_name = "проверка источника"
        verbose_name_plural = "проверки источников"
        ordering = ["-date", "source"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "date", "check_type"], name="health_check_unique_per_day"
            ),
        ]

    def __str__(self):
        return f"{self.source} {self.date} {self.check_type}: {self.status}"


class GoldenPage(models.Model):
    """Контрольная страница с вручную зафиксированным эталоном извлечения."""

    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.CASCADE, related_name="golden_pages"
    )
    url = models.URLField("URL", max_length=2000)
    expected_title = models.TextField("эталон: заголовок")
    expected_published_at = models.DateTimeField("эталон: дата", null=True, blank=True)
    expected_text_prefix = models.TextField("эталон: начало текста (~200 символов)")
    is_active = models.BooleanField("используется", default=True)
    last_checked_at = models.DateTimeField("последняя проверка", null=True, blank=True)
    last_status = models.CharField(
        "последний результат", max_length=10, choices=Level.choices, blank=True
    )

    class Meta:
        verbose_name = "контрольная страница"
        verbose_name_plural = "контрольные страницы"
        constraints = [
            models.UniqueConstraint(fields=["source", "url"], name="golden_page_unique_url"),
        ]

    def __str__(self):
        return self.url


class SourceIncident(models.Model):
    """Инцидент сбора. Нужен и для уведомлений, и для учёта пробелов в анализе.

    Повторные сигналы того же типа объединяются: открытый инцидент на «источник × тип» один.
    """

    class Type(models.TextChoices):
        NO_DOCUMENTS = "no_documents", "Нет документов за сутки"
        LOW_VOLUME = "low_volume", "Объём ниже нормы"
        STALE_FEED = "stale_feed", "Лента не обновляется"
        EXTRACTION = "extraction", "Падение качества извлечения"
        BOILERPLATE = "boilerplate", "Одинаковый текст у разных URL"
        LANGUAGE = "language", "Язык не совпадает"
        GOLDEN_MISMATCH = "golden_mismatch", "Расхождение с контрольными страницами"
        SELECTOR_MISSING = "selector_missing", "Пропал селектор"
        HTTP_ERRORS = "http_errors", "Ошибки HTTP / редиректы / таймауты"
        BLOCKED = "blocked", "Капча / антибот"
        CHANNEL_UNAVAILABLE = "channel_unavailable", "Канал недоступен"
        QUALITY_DROP = "quality_drop", "Падение средней оценки качества"

    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.CASCADE, related_name="incidents"
    )
    incident_type = models.CharField("тип", max_length=20, choices=Type.choices)
    level = models.CharField(
        "уровень",
        max_length=10,
        choices=[(Level.WARNING, Level.WARNING.label), (Level.CRITICAL, Level.CRITICAL.label)],
    )
    started_at = models.DateTimeField("начало")
    ended_at = models.DateTimeField("конец", null=True, blank=True, help_text="Пусто — открыт.")
    description = models.TextField("описание", blank=True)
    examples = models.JSONField(
        "примеры", default=list, blank=True, help_text="URL или ID проблемных документов."
    )
    occurrences = models.PositiveIntegerField("повторов сигнала", default=1)

    class Meta:
        verbose_name = "инцидент сбора"
        verbose_name_plural = "инциденты сбора"
        ordering = ["-started_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="incident_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["source", "incident_type"],
                condition=Q(ended_at__isnull=True),
                name="incident_one_open_per_type",
            ),
        ]

    def __str__(self):
        return f"{self.source}: {self.get_incident_type_display()} ({self.level})"
