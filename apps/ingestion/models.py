"""Мониторинг сбора: проверки здоровья, контрольные страницы, инциденты (README §4.2)."""

from django.db import models
from django.db.models import F, Q

from apps.actors.models import Source


class Level(models.TextChoices):
    OK = "ok", "V normě"
    WARNING = "warning", "Varování"
    CRITICAL = "critical", "Kritické"


class SourceFeed(models.Model):
    """RSS/Atom-лента, sitemap или страница-список статей источника.

    У медиа — по ленте на политическую рубрику. Страница-список — для сайтов без
    пригодной ленты (42tcen.com: RSS пуст, вложенные sitemap запрещены в robots.txt);
    ссылки на статьи отбираются по collector_config["listing_link_pattern"].
    """

    class Kind(models.TextChoices):
        RSS = "rss", "RSS / Atom"
        SITEMAP = "sitemap", "Sitemap"
        LISTING = "listing", "Stránka s výpisem článků"

    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.CASCADE, related_name="feeds"
    )
    url = models.URLField("adresa kanálu", max_length=1000)
    kind = models.CharField("typ", max_length=10, choices=Kind.choices, default=Kind.RSS)
    rubric = models.CharField("rubrika", max_length=200, blank=True)
    is_active = models.BooleanField("používá se", default=True)

    # Условные запросы (304 Not Modified) и состояние для страницы «Источники».
    etag = models.CharField(max_length=300, blank=True, editable=False)
    last_modified = models.CharField(max_length=100, blank=True, editable=False)
    last_fetched_at = models.DateTimeField("poslední požadavek", null=True, blank=True)
    last_success_at = models.DateTimeField("poslední úspěšné stažení", null=True, blank=True)
    last_item_count = models.PositiveIntegerField("položek v kanálu", null=True, blank=True)
    last_error = models.TextField("poslední chyba", blank=True)

    class Meta:
        verbose_name = "kanál zdroje"
        verbose_name_plural = "kanály zdrojů"
        constraints = [
            models.UniqueConstraint(fields=["source", "url"], name="source_feed_unique_url"),
        ]

    def __str__(self):
        return f"{self.source.name}: {self.rubric or self.url}"


class CollectionRun(models.Model):
    """Один запуск сбора по источнику: что найдено и сохранено. Основа мониторинга (§4.2)."""

    class Status(models.TextChoices):
        RUNNING = "running", "Probíhá"
        OK = "ok", "Úspěšně"
        PARTIAL = "partial", "S chybami"
        FAILED = "failed", "Chyba"

    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.CASCADE, related_name="collection_runs"
    )
    started_at = models.DateTimeField("začátek", auto_now_add=True)
    finished_at = models.DateTimeField("konec", null=True, blank=True)
    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.RUNNING)
    found = models.PositiveIntegerField("nalezeno", default=0)
    new = models.PositiveIntegerField("nových", default=0)
    versions = models.PositiveIntegerField("nových verzí", default=0)
    unchanged = models.PositiveIntegerField("beze změn", default=0)
    duplicates = models.PositiveIntegerField("duplikátů podle textu", default=0)
    skipped = models.PositiveIntegerField("přeskočeno", default=0)
    errors = models.PositiveIntegerField("chyb", default=0)
    http_requests = models.PositiveIntegerField("HTTP požadavků", default=0)
    details = models.JSONField(
        "podrobnosti", default=dict, blank=True, help_text="Příklady chyb, stavové kódy HTTP."
    )

    class Meta:
        verbose_name = "spuštění sběru"
        verbose_name_plural = "spuštění sběru"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "-started_at"])]

    def __str__(self):
        return f"{self.source.name} {self.started_at:%Y-%m-%d %H:%M}: {self.status}"


class SourceHealthCheck(models.Model):
    """Результат одной проверки мониторинга по источнику за день."""

    class CheckType(models.TextChoices):
        VOLUME = "volume", "Objem"
        EXTRACTION = "extraction", "Kvalita extrakce"
        GOLDEN = "golden", "Kontrolní stránky"
        SELECTORS = "selectors", "Strukturní otisk"
        HTTP = "http", "HTTP / RSS / dostupnost"
        QUALITY = "quality", "Průměrné hodnocení kvality"

    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.CASCADE, related_name="health_checks"
    )
    date = models.DateField("den")
    check_type = models.CharField("kontrola", max_length=12, choices=CheckType.choices)
    status = models.CharField("výsledek", max_length=10, choices=Level.choices)
    metrics = models.JSONField(
        "metriky", default=dict, blank=True, help_text="Např.: n, medián, podíl bez data."
    )
    details = models.TextField("podrobnosti", blank=True)
    checked_at = models.DateTimeField("zkontrolováno", auto_now=True)

    class Meta:
        verbose_name = "kontrola zdroje"
        verbose_name_plural = "kontroly zdrojů"
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
        Source, verbose_name="zdroj", on_delete=models.CASCADE, related_name="golden_pages"
    )
    url = models.URLField("URL", max_length=2000)
    expected_title = models.TextField("vzor: titulek")
    expected_published_at = models.DateTimeField("vzor: datum", null=True, blank=True)
    expected_text_prefix = models.TextField("vzor: začátek textu (~200 znaků)")
    is_active = models.BooleanField("používá se", default=True)
    last_checked_at = models.DateTimeField("poslední kontrola", null=True, blank=True)
    last_status = models.CharField(
        "poslední výsledek", max_length=10, choices=Level.choices, blank=True
    )

    class Meta:
        verbose_name = "kontrolní stránka"
        verbose_name_plural = "kontrolní stránky"
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
        NO_DOCUMENTS = "no_documents", "Žádné dokumenty za den"
        LOW_VOLUME = "low_volume", "Objem pod normou"
        STALE_FEED = "stale_feed", "Kanál se neaktualizuje"
        EXTRACTION = "extraction", "Pokles kvality extrakce"
        BOILERPLATE = "boilerplate", "Stejný text u různých URL"
        LANGUAGE = "language", "Jazyk neodpovídá"
        GOLDEN_MISMATCH = "golden_mismatch", "Neshoda s kontrolními stránkami"
        SELECTOR_MISSING = "selector_missing", "Chybí selektor"
        HTTP_ERRORS = "http_errors", "Chyby HTTP / přesměrování / časové limity"
        BLOCKED = "blocked", "Captcha / antibot"
        CHANNEL_UNAVAILABLE = "channel_unavailable", "Kanál není dostupný"
        QUALITY_DROP = "quality_drop", "Pokles průměrného hodnocení kvality"

    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.CASCADE, related_name="incidents"
    )
    incident_type = models.CharField("typ", max_length=20, choices=Type.choices)
    level = models.CharField(
        "úroveň",
        max_length=10,
        choices=[(Level.WARNING, Level.WARNING.label), (Level.CRITICAL, Level.CRITICAL.label)],
    )
    started_at = models.DateTimeField("začátek")
    ended_at = models.DateTimeField("konec", null=True, blank=True, help_text="Prázdné — otevřený.")
    description = models.TextField("popis", blank=True)
    examples = models.JSONField(
        "příklady", default=list, blank=True, help_text="URL nebo ID problémových dokumentů."
    )
    occurrences = models.PositiveIntegerField("opakování signálu", default=1)

    class Meta:
        verbose_name = "incident sběru"
        verbose_name_plural = "incidenty sběru"
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
