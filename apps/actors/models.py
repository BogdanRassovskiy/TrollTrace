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
    SECURITY = "I", "I. Instituce bezpečnosti"


# Слой I говорит *о* нарративах (метауровень) и в общие агрегаты по умолчанию не входит
# (README §2.1). Агрегаты, сравнения слоёв и воронки по умолчанию берут только эти слои.
ANALYSIS_LAYERS = [layer for layer in Layer.values if layer != Layer.SECURITY]


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
        INSTITUTION = "institution", "Instituce bezpečnosti"
        OFFICIAL = "official", "Profesní vedoucí bezpečnostní instituce"

    class Status(models.TextChoices):
        ACTIVE = "active", "Aktivní"
        EXCLUDED = "excluded", "Vyřazen"

    class OfficeStatus(models.TextChoices):
        OFFICE = "office", "Zastává veřejnou funkci"
        PARTY_ONLY = "party_only", "Jen stranická funkce, bez veřejné funkce"
        NONE = "none", "Bez funkce (bývalý politik nebo veřejná osoba)"

    class ActivityCohort(models.TextChoices):
        LOW = "low", "Nízká aktivita"
        MEDIUM = "medium", "Střední aktivita"
        HIGH = "high", "Vysoká aktivita"

    name = models.CharField("jméno / název", max_length=200)
    actor_type = models.CharField("typ", max_length=20, choices=Type.choices)
    layer = models.CharField(
        "vrstva",
        max_length=1,
        choices=Layer.choices,
        blank=True,
        help_text="U médií, sítí a institucí vrstva, ve které působí. U politiků může zůstat "
        "prázdná (europoslanci, členové vlády mimo Sněmovnu): vrstvu dokumentu určuje zdroj.",
    )
    party = models.CharField("strana", max_length=100, blank=True)
    role = models.CharField(
        "funkce",
        max_length=300,
        blank=True,
        help_text="Souhrn současných funkcí (ActorPosition); doplňuje se automaticky.",
    )
    is_hybrid = models.BooleanField(
        "hybridní aktér",
        default=False,
        help_text="Formálně politik, komunikací blíže alternativní scéně.",
    )
    office_status = models.CharField(
        "současná funkce",
        max_length=12,
        choices=OfficeStatus.choices,
        blank=True,
        db_index=True,
        help_text="Doplňuje se automaticky z funkcí. Bývalý politik zůstává politikem, ale "
        "musí být jasně vidět, že teď žádnou skutečnou funkci nezastává.",
    )
    manual_inclusion = models.BooleanField(
        "zařazen rozhodnutím výzkumníka",
        default=False,
        help_text="Politik nebo veřejná osoba zařazená bez současné funkce podle institucionální "
        "definice (bývalý prezident, starosta malé obce, často citovaný bývalý politik). "
        "Důvod uveďte v „důvod zařazení“.",
    )
    added_automatically = models.BooleanField(
        "přidán automaticky",
        default=False,
        db_index=True,
        help_text="Starosta, zastupitel apod., jehož funkce je výslovně uvedena v textu "
        "dokumentu (viz zmínky). Neověřeno výzkumníkem: chybně rozpoznané vyřaďte.",
    )
    security_portfolio = models.BooleanField(
        "bezpečnostní resort",
        default=False,
        help_text="Ministr vnitra, obrany nebo zahraničí: jeho politická komunikace se "
        "porovnává s resortní (vrstva I, README §2.1).",
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

    @property
    def holds_public_office(self) -> bool:
        """Veřejná funkce (poslanec, europoslanec, člen vlády, senátor, vedoucí instituce).
        Předseda neparlamentní strany je politik, ale veřejnou funkci nezastává."""
        return self.positions.filter(
            end_date__isnull=True, kind__in=ActorPosition.PUBLIC_OFFICE_KINDS
        ).exists()

    def refresh_role(self, save: bool = True) -> str:
        """Souhrn současných funkcí: člověk může zastávat několik najednou.

        Členství a funkce ve výborech Sněmovny jsou jen informativní a do souhrnu nejdou.
        U politika bez veřejné funkce se to v souhrnu výslovně uvádí.
        """
        positions = self.positions.filter(end_date__isnull=True).exclude(
            kind=ActorPosition.Kind.CHAMBER
        )
        titles = []
        for position in sorted(positions, key=lambda p: ActorPosition.SUMMARY_ORDER[p.kind]):
            if position.title not in titles:
                titles.append(position.title)
        if not titles:
            last = (
                self.positions.filter(end_date__isnull=False)
                .exclude(kind=ActorPosition.Kind.CHAMBER)
                .order_by("-end_date")
                .first()
            )
            if last is not None:
                titles.append(f"dříve {last.title}")
        office = self.holds_public_office
        if self.actor_type == self.Type.POLITICIAN and not office:
            titles.append("bez veřejné funkce")
        if office:
            status = self.OfficeStatus.OFFICE
        elif positions.exists():
            status = self.OfficeStatus.PARTY_ONLY
        else:
            status = self.OfficeStatus.NONE
        if self.actor_type not in {self.Type.POLITICIAN, self.Type.OFFICIAL}:
            status = ""  # média, kanály, instituce: pojem funkce se na ně nevztahuje
        role = "; ".join(titles)[:300] if titles else self.role
        if (role, status) != (self.role, self.office_status):
            self.role, self.office_status = role, status
            if save:
                self.save(update_fields=["role", "office_status", "updated_at"])
        return self.role

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


class ActorPosition(models.Model):
    """Funkce aktéra v čase. Jeden člověk může zastávat několik funkcí současně
    (poslanec a ministr, europoslanec a předseda strany) — README §2 «Акторы»."""

    class Kind(models.TextChoices):
        DEPUTY = "deputy", "Poslanec PSP"
        CHAMBER_LEADERSHIP = "chamber_leadership", "Vedení Sněmovny"
        CHAMBER = "chamber", "Funkce a členství v orgánech Sněmovny (informativně)"
        MEP = "mep", "Europoslanec"
        GOVERNMENT = "government", "Člen vlády"
        PRESIDENT = "president", "Prezident republiky"
        SENATOR = "senator", "Senátor"
        REGIONAL = "regional", "Krajská nebo obecní funkce"
        OFFICIAL = "official", "Vedoucí bezpečnostní instituce"
        PARTY_LEADER = "party_leader", "Předseda strany"
        PARTY_FUNCTION = "party_function", "Jiná stranická funkce"
        OTHER = "other", "Jiná funkce"

    # Veřejné funkce. Stranické funkce politika nevylučují, ale veřejnou funkcí nejsou.
    PUBLIC_OFFICE_KINDS = [
        Kind.DEPUTY, Kind.CHAMBER_LEADERSHIP, Kind.MEP, Kind.GOVERNMENT, Kind.PRESIDENT,
        Kind.SENATOR, Kind.REGIONAL, Kind.OFFICIAL,
    ]  # fmt: skip
    SUMMARY_ORDER = {kind: index for index, kind in enumerate(Kind.values)}

    actor = models.ForeignKey(
        Actor, verbose_name="aktér", on_delete=models.CASCADE, related_name="positions"
    )
    kind = models.CharField("druh", max_length=20, choices=Kind.choices)
    title = models.CharField("funkce", max_length=200)
    organization = models.CharField("orgán / strana", max_length=200, blank=True)
    start_date = models.DateField("od", null=True, blank=True)
    end_date = models.DateField(
        "do", null=True, blank=True, help_text="Prázdné — funkci zastává dosud."
    )
    external_id = models.CharField(
        "ID v registru", max_length=100, blank=True, help_text="Např. persId Evropského parlamentu."
    )
    source_url = models.URLField("ověřeno na", max_length=500, blank=True)
    note = models.TextField("poznámka", blank=True)

    class Meta:
        verbose_name = "funkce aktéra"
        verbose_name_plural = "funkce aktérů"
        ordering = ["actor", "-end_date", "start_date"]
        indexes = [models.Index(fields=["kind", "end_date"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True)
                | Q(start_date__isnull=True)
                | Q(end_date__gte=F("start_date")),
                name="position_end_after_start",
            ),
        ]

    def __str__(self):
        period = f"{self.start_date or '?'} – {self.end_date or 'dosud'}"
        return f"{self.actor}: {self.title} ({period})"


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


class ActorMention(models.Model):
    """Zmínka o aktérovi a jeho funkci v textu dokumentu: doklad automatického přidání."""

    actor = models.ForeignKey(
        Actor, verbose_name="aktér", on_delete=models.CASCADE, related_name="mentions"
    )
    document = models.ForeignKey(
        "documents.Document",
        verbose_name="dokument",
        on_delete=models.CASCADE,
        related_name="actor_mentions",
    )
    title = models.CharField("funkce uvedená v textu", max_length=200)
    quote = models.TextField("citace")
    created_at = models.DateTimeField("nalezeno", auto_now_add=True)

    class Meta:
        verbose_name = "zmínka v textu"
        verbose_name_plural = "zmínky v textu"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["actor", "document", "title"], name="actor_mention_unique"
            ),
        ]

    def __str__(self):
        return f"{self.actor}: {self.title}"
