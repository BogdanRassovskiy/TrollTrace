"""Темы, версионируемый кодбук нарративов, метки, опровержения, кандидаты (README §3, §9)."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.actors.models import Layer
from apps.core.models import TimeStampedModel, VersionedModel, VersionRecord
from apps.documents.models import Document


class Stance(models.TextChoices):
    """Метка = нарратив + позиция (README §3)."""

    PRO = "pro", "Pro"
    CONTRA = "contra", "Proti"
    NEUTRAL = "neutral", "Neutrálně"


class Topic(TimeStampedModel):
    """Стабильная тема. «Евро» — тема, «евро разорит чехов» — нарратив."""

    code = models.SlugField("kód", max_length=50, unique=True)
    name = models.CharField("název", max_length=200)
    description = models.TextField("popis", blank=True)

    class Meta:
        verbose_name = "téma"
        verbose_name_plural = "témata"
        ordering = ["name"]

    def __str__(self):
        return self.name


class NarrativeStatus(models.TextChoices):
    CANDIDATE = "candidate", "Kandidát"
    ACTIVE = "active", "Aktivní"
    ARCHIVED = "archived", "Archiv"


class Narrative(VersionedModel):
    """Нарратив кодбука. Любое изменение определения создаёт новую NarrativeVersion."""

    VERSIONED_FIELDS = ("topic", "name", "definition", "examples", "status")

    code = models.SlugField("ID", max_length=50, unique=True)
    topic = models.ForeignKey(
        Topic, verbose_name="téma", on_delete=models.PROTECT, related_name="narratives"
    )
    name = models.CharField("stručná formulace", max_length=300)
    definition = models.TextField("definice")
    examples = models.JSONField("příklady", default=list, blank=True)
    status = models.CharField(
        "stav", max_length=10, choices=NarrativeStatus.choices, default=NarrativeStatus.ACTIVE
    )
    added_at = models.DateTimeField("přidán do kódové knihy", auto_now_add=True)

    class Meta:
        verbose_name = "narativ"
        verbose_name_plural = "narativy"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code}: {self.name}"

    def create_version_record(self, snapshot, *, changed_by, change_note):
        NarrativeVersion.objects.create(
            narrative=self,
            version=self.current_version,
            changed_by=changed_by,
            change_note=change_note,
            **snapshot,
        )

    @property
    def latest_version(self):
        return self.versions.get(version=self.current_version)


class NarrativeVersion(VersionRecord):
    """Снимок нарратива. Метки ссылаются на конкретную версию — это и есть «версия кодбука»."""

    narrative = models.ForeignKey(
        Narrative, verbose_name="narativ", on_delete=models.CASCADE, related_name="versions"
    )
    topic = models.ForeignKey(
        Topic, verbose_name="téma", on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField("stručná formulace", max_length=300)
    definition = models.TextField("definice")
    examples = models.JSONField("příklady", default=list, blank=True)
    status = models.CharField("stav", max_length=10, choices=NarrativeStatus.choices)

    class Meta(VersionRecord.Meta):
        verbose_name = "verze narativu"
        verbose_name_plural = "historie kódové knihy"
        constraints = [
            models.UniqueConstraint(
                fields=["narrative", "version"], name="narrative_version_unique_number"
            ),
        ]

    def __str__(self):
        return f"{self.narrative.code} v{self.version}"


class Label(models.Model):
    """Метка документа: нарратив + позиция, кто поставил и по какой версии кодбука."""

    class Origin(models.TextChoices):
        MODEL = "model", "Model"
        HUMAN = "human", "Člověk"

    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="labels"
    )
    narrative = models.ForeignKey(
        Narrative, verbose_name="narativ", on_delete=models.PROTECT, related_name="labels"
    )
    narrative_version = models.ForeignKey(
        NarrativeVersion,
        verbose_name="verze kódové knihy",
        on_delete=models.PROTECT,
        related_name="labels",
    )
    stance = models.CharField("postoj", max_length=10, choices=Stance.choices)
    origin = models.CharField("zdroj štítku", max_length=10, choices=Origin.choices)
    confidence = models.FloatField("jistota", null=True, blank=True)
    model_name = models.CharField("model", max_length=100, blank=True)
    annotator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="anotátor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("vytvořen", auto_now_add=True)

    class Meta:
        verbose_name = "štítek"
        verbose_name_plural = "štítky"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "narrative_version", "origin"],
                name="label_unique_per_version_origin",
            ),
            models.CheckConstraint(
                condition=Q(confidence__isnull=True) | Q(confidence__gte=0, confidence__lte=1),
                name="label_confidence_0_1",
            ),
        ]
        indexes = [models.Index(fields=["narrative", "stance"])]

    def __str__(self):
        return f"{self.document_id} → {self.narrative_version} ({self.stance})"

    def save(self, *args, **kwargs):
        if self.narrative_id is None and self.narrative_version_id is not None:
            self.narrative_id = self.narrative_version.narrative_id
        self.clean()
        super().save(*args, **kwargs)

    def clean(self):
        if self.narrative_version_id and self.narrative_version.narrative_id != self.narrative_id:
            raise ValidationError("Verze kódové knihy patří k jinému narativu.")


class Debunk(models.Model):
    """Внешнее опровержение на уровне нарратива. Собственного фактчекинга нет (README §1)."""

    class Organization(models.TextChoices):
        DEMAGOG = "demagog", "Demagog.cz"
        MANIPULATORI = "manipulatori", "Manipulátoři"
        AFP = "afp", "AFP Fakty"
        EUVSDISINFO = "euvsdisinfo", "EUvsDisinfo"
        OTHER = "other", "Jiná"

    narrative = models.ForeignKey(
        Narrative, verbose_name="narativ", on_delete=models.CASCADE, related_name="debunks"
    )
    organization = models.CharField("organizace", max_length=20, choices=Organization.choices)
    url = models.URLField("odkaz", max_length=1000)
    title = models.CharField("titulek", max_length=500, blank=True)
    published_at = models.DateField("datum publikace", null=True, blank=True)

    class Meta:
        verbose_name = "vyvrácení"
        verbose_name_plural = "vyvrácení"
        constraints = [
            models.UniqueConstraint(fields=["narrative", "url"], name="debunk_unique_url"),
        ]

    def __str__(self):
        return f"{self.get_organization_display()}: {self.title or self.url}"


class NarrativeCandidate(TimeStampedModel):
    """Кандидат в нарративы из индуктивной кластеризации. Решение принимает исследователь."""

    class Status(models.TextChoices):
        NEW = "new", "Nový"
        ACCEPTED = "accepted", "Přijat"
        MERGED = "merged", "Sloučen"
        REJECTED = "rejected", "Zamítnut"
        DEFERRED = "deferred", "Odložen"

    description = models.TextField("popis shluku")
    keywords = models.JSONField("klíčová slova", default=list, blank=True)
    example_documents = models.ManyToManyField(
        Document, verbose_name="příklady", blank=True, related_name="+"
    )
    nearest_narratives = models.ManyToManyField(
        Narrative, verbose_name="nejbližší narativy", blank=True, related_name="+"
    )
    n_documents = models.PositiveIntegerField("dokumentů")
    n_authors = models.PositiveIntegerField("nezávislých autorů")
    window_start = models.DateField("okno: od")
    window_end = models.DateField("okno: do")
    layers = models.JSONField(
        "vrstvy", default=list, blank=True, help_text=f"Kódy vrstev: {', '.join(Layer.values)}."
    )

    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.NEW)
    decision_reason = models.TextField("důvod rozhodnutí", blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="kdo rozhodl",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField("kdy rozhodnuto", null=True, blank=True)
    resulting_narrative = models.ForeignKey(
        Narrative,
        verbose_name="vytvořený / cílový narativ",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_candidates",
    )

    class Meta:
        verbose_name = "kandidát na narativ"
        verbose_name_plural = "kandidáti na narativy"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(window_end__gte=models.F("window_start")),
                name="candidate_window_valid",
            ),
            # Принятый или объединённый кандидат обязан указывать на нарратив.
            models.CheckConstraint(
                condition=~Q(status__in=["accepted", "merged"])
                | Q(resulting_narrative__isnull=False),
                name="candidate_decision_has_narrative",
            ),
        ]

    def __str__(self):
        return self.description[:80]
