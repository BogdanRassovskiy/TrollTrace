"""Источники, акторы, периоды включения в выборку и привязка аккаунтов (README §2, §9)."""

import datetime

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models, transaction
from django.db.models import F, Func, Q
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel


class Layer(models.TextChoices):
    """Слои данных (README §2). Объёмы между слоями напрямую не сравниваются."""

    ATTRIBUTED = "A", "A. Atribuované sítě"
    ALTERNATIVE = "B", "B. Alternativní média"
    TELEGRAM = "C", "C. Telegram"
    MAINSTREAM = "D", "D. Mainstreamová média"
    PARLIAMENT = "E", "E. Parlament"
    META = "F", "F. Facebook / Instagram"
    TIKTOK = "G", "G. TikTok"
    X = "H", "H. X"


class Platform(models.TextChoices):
    WEB = "web", "Web"
    TELEGRAM = "telegram", "Telegram"
    PARLIAMENT = "psp", "Stenoprotokoly PSP"
    FACEBOOK = "facebook", "Facebook"
    INSTAGRAM = "instagram", "Instagram"
    TIKTOK = "tiktok", "TikTok"
    X = "x", "X"


class DateRange(Func):
    """daterange(start, end, '[]'): end = NULL означает открытый период."""

    function = "daterange"
    output_field = DateRangeField()


class Source(TimeStampedModel):
    """Площадка: сайт, Telegram-канал, стенограммы. Атрибуция — только внешняя (README §1)."""

    name = models.CharField("název", max_length=200)
    layer = models.CharField("vrstva", max_length=1, choices=Layer.choices)
    platform = models.CharField("platforma", max_length=20, choices=Platform.choices)
    url = models.URLField("adresa", max_length=500, unique=True)
    is_active = models.BooleanField("sběr zapnut", default=True)

    # Параметры сбора (README §4.1). Ленты и sitemap — SourceFeed (apps.ingestion).
    collect_interval_minutes = models.PositiveIntegerField("interval sběru (min)", default=30)
    request_delay_seconds = models.FloatField("pauza mezi požadavky (s)", default=2.0)
    collector_config = models.JSONField(
        "nastavení kolektoru",
        default=dict,
        blank=True,
        help_text="Např.: max_articles, backfill_pages, term (PSP).",
    )

    # Атрибуция и внешний рейтинг — ссылки на чужие оценки, не наш вывод.
    attribution_by = models.CharField(
        "atribuce: kdo",
        max_length=200,
        blank=True,
        help_text="Státní orgán nebo výzkumná organizace.",
    )
    attribution_url = models.URLField("atribuce: odkaz na zprávu", max_length=500, blank=True)
    external_rating = models.CharField("externí hodnocení", max_length=100, blank=True)
    external_rating_source = models.CharField("externí hodnocení: kdo", max_length=200, blank=True)

    # Параметры мониторинга сбора (README §4.2), настраиваются per source.
    expected_language = models.CharField(
        "očekávaný jazyk",
        max_length=8,
        default="cs",
        blank=True,
        help_text="Prázdné — jazyk se nekontroluje (zdroj publikuje ve více jazycích).",
    )
    min_text_length = models.PositiveIntegerField("minimální délka textu", default=300)
    stale_after_hours = models.PositiveIntegerField(
        "upozornit, pokud nejsou nové dokumenty (h)", default=48
    )
    notes = models.TextField("poznámky", blank=True)

    class Meta:
        verbose_name = "zdroj"
        verbose_name_plural = "zdroje"
        ordering = ["layer", "name"]

    def __str__(self):
        return f"{self.name} ({self.layer})"


class Actor(TimeStampedModel):
    """Автор документов: политик, медиа, канал, атрибутированная сеть (README §2 «Акторы»)."""

    class Type(models.TextChoices):
        POLITICIAN = "politician", "Politik"
        INFLUENCER = "influencer", "Politický influencer"
        MEDIA = "media", "Médium"
        CHANNEL = "channel", "Kanál"
        ATTRIBUTED_NETWORK = "attributed_network", "Atribuovaná síť"

    class Status(models.TextChoices):
        ACTIVE = "active", "Aktivní"
        EXCLUDED = "excluded", "Vyřazen"

    class ActivityCohort(models.TextChoices):
        LOW = "low", "Nízká aktivita"
        MEDIUM = "medium", "Střední aktivita"
        HIGH = "high", "Vysoká aktivita"

    name = models.CharField("jméno / název", max_length=200)
    actor_type = models.CharField("typ", max_length=20, choices=Type.choices)
    layer = models.CharField("vrstva", max_length=1, choices=Layer.choices)
    party = models.CharField("strana", max_length=100, blank=True)
    role = models.CharField(
        "role",
        max_length=100,
        blank=True,
        help_text="Poslanec PSP, europoslanec, ministr, předseda strany…",
    )
    is_hybrid = models.BooleanField(
        "hybridní aktér",
        default=False,
        help_text="Formálně politik, komunikací blíže alternativní scéně.",
    )
    activity_cohort = models.CharField(
        "kohorta aktivity", max_length=10, choices=ActivityCohort.choices, blank=True
    )
    photo = models.ImageField("foto (ikona)", upload_to="actors/", blank=True)
    photo_source_url = models.URLField("foto: zdroj", max_length=500, blank=True)
    is_pseudonymized = models.BooleanField(
        "pseudonymizován", default=False, help_text="Soukromé osoby (GDPR, README §6)."
    )
    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.ACTIVE)
    inclusion_criterion = models.TextField("důvod zařazení", blank=True)
    inclusion_reference = models.URLField("důvod: odkaz", max_length=500, blank=True)

    class Meta:
        verbose_name = "aktér"
        verbose_name_plural = "aktéři"
        ordering = ["name"]
        indexes = [models.Index(fields=["layer", "status"]), models.Index(fields=["party"])]

    def __str__(self):
        return self.name

    @property
    def open_period(self):
        return self.inclusion_periods.filter(end_date__isnull=True).first()

    def include(self, reason: str = "", on: datetime.date | None = None):
        """Открывает новый период включения. Старые периоды не затираются (README §7.2)."""
        with transaction.atomic():
            period = ActorInclusionPeriod.objects.create(
                actor=self, start_date=on or datetime.date.today(), inclusion_reason=reason
            )
            self.status = self.Status.ACTIVE
            self.save(update_fields=["status", "updated_at"])
        return period

    def exclude(self, reason: str, on: datetime.date | None = None):
        """Безопасное «удаление»: закрывает открытый период, документы и метки остаются."""
        with transaction.atomic():
            period = self.open_period
            if period is not None:
                period.end_date = on or datetime.date.today()
                period.exclusion_reason = reason
                period.save(update_fields=["end_date", "exclusion_reason"])
            self.status = self.Status.EXCLUDED
            self.save(update_fields=["status", "updated_at"])
        return period


class ActorInclusionPeriod(models.Model):
    """Период, когда актор входил в выборку. Доли считаются с учётом периодов (README §7.2)."""

    actor = models.ForeignKey(
        Actor, verbose_name="aktér", on_delete=models.CASCADE, related_name="inclusion_periods"
    )
    start_date = models.DateField("od")
    end_date = models.DateField(
        "do", null=True, blank=True, help_text="Prázdné — období je otevřené."
    )
    inclusion_reason = models.TextField("důvod zařazení", blank=True)
    exclusion_reason = models.TextField("důvod vyřazení", blank=True)

    class Meta:
        verbose_name = "období zařazení"
        verbose_name_plural = "období zařazení"
        ordering = ["actor", "start_date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="inclusion_period_end_after_start",
            ),
            # Периоды одного актора не пересекаются; заодно открытый период может быть только один.
            ExclusionConstraint(
                name="inclusion_periods_no_overlap",
                expressions=[
                    ("actor", RangeOperators.EQUAL),
                    (
                        DateRange("start_date", "end_date", models.Value("[]")),
                        RangeOperators.OVERLAPS,
                    ),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.actor}: {self.start_date} — {self.end_date or '…'}"


class ActorAccount(models.Model):
    """Привязка актора к источнику: handle Telegram, URL профиля, рубрика сайта и т. п."""

    actor = models.ForeignKey(
        Actor, verbose_name="aktér", on_delete=models.CASCADE, related_name="accounts"
    )
    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.PROTECT, related_name="accounts"
    )
    account_id = models.CharField(
        "identifikátor účtu",
        max_length=300,
        blank=True,
        help_text="Handle, ID kanálu nebo cesta profilu. Prázdné — aktér je celý zdroj.",
    )
    url = models.URLField("odkaz", max_length=500, blank=True)
    added_at = models.DateTimeField("přidán", auto_now_add=True)

    class Meta:
        verbose_name = "účet aktéra"
        verbose_name_plural = "účty aktérů"
        constraints = [
            # Один аккаунт не может принадлежать двум акторам (без учёта регистра handle).
            models.UniqueConstraint(
                "source", Lower("account_id"), name="actor_account_unique_per_source"
            ),
        ]

    def __str__(self):
        return f"{self.actor} @ {self.source}: {self.account_id or '*'}"

    def save(self, *args, **kwargs):
        self.account_id = self.account_id.strip().removeprefix("@")
        super().save(*args, **kwargs)
