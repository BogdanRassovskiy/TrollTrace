"""Темы, версионируемый кодбук нарративов, метки, опровержения, кандидаты (README §3, §9)."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Exists, OuterRef, Q
from pgvector.django import VectorField

from apps.actors.models import Layer
from apps.core.models import TimeStampedModel, VersionedModel, VersionRecord
from apps.documents.models import EMBEDDING_DIMENSIONS, Document


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
    keywords = models.JSONField(
        "klíčová slova",
        default=list,
        blank=True,
        help_text="Začátky slov bez diakritiky, např. „migra“, „ochran hranic“; "
        "„=nato“ — jen celé slovo. Filtr relevance.",
    )

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


class NarrativeKind(models.TextChoices):
    NARRATIVE = "narrative", "Narativ"
    THEME = "theme", "Téma vlivu (ne zformulovaný narativ)"


class Narrative(VersionedModel):
    """Нарратив кодбука. Любое изменение определения создаёт новую NarrativeVersion."""

    VERSIONED_FIELDS = (
        "topic", "kind", "name", "definition", "examples", "counter_examples", "stance_guide",
        "cues", "attribution", "status",
    )  # fmt: skip

    code = models.SlugField("ID", max_length=50, unique=True)
    topic = models.ForeignKey(
        Topic, verbose_name="téma", on_delete=models.PROTECT, related_name="narratives"
    )
    name = models.CharField("stručná formulace", max_length=300)
    definition = models.TextField("definice")
    examples = models.JSONField(
        "příklady (postoj pro)",
        default=list,
        blank=True,
        help_text="Typické formulace, které narativ šíří.",
    )
    kind = models.CharField(
        "druh", max_length=10, choices=NarrativeKind.choices, default=NarrativeKind.NARRATIVE
    )
    counter_examples = models.JSONField(
        "příklady (postoj proti)",
        default=list,
        blank=True,
        help_text="Formulace, které narativ vyvracejí nebo označují za propagandu.",
    )
    stance_guide = models.JSONField(
        "vodítko k postojům",
        default=dict,
        blank=True,
        help_text='Co znamená postoj pro / proti / neutrálně a co narativem není: {"pro": …, '
        '"contra": …, "neutral": …, "absent": …}. Narativ se určuje přísně: „proti“ je '
        "výslovná reakce na tvrzení, ne opačný názor na téma. Pro anotátory i pro model.",
    )
    cues = models.JSONField(
        "signální slova",
        default=list,
        blank=True,
        help_text="Začátky slov bez diakritiky jako u témat („sabotaz“, „=sssr“). Text s více "
        "signálními slovy stačí k zařazení při nižší podobnosti.",
    )
    attribution = models.TextField(
        "zdroj a atribuce",
        blank=True,
        help_text="Kdo narativ popsal a zda ho připisuje Rusku (např. BIS, výroční zpráva).",
    )
    status = models.CharField(
        "stav",
        max_length=10,
        choices=NarrativeStatus.choices,
        default=NarrativeStatus.ACTIVE,
        help_text="Aktivní i kandidát se sledují v nových dokumentech; do analýzy patří jen "
        "aktivní. Archivovaný se nesleduje.",
    )
    discovered = models.BooleanField(
        "navržen automaticky",
        default=False,
        help_text="Vznikl ze shluku nových tvrzení v dokumentech (kandidát na narativ), "
        "ne z literatury nebo zpráv.",
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
    examples = models.JSONField("příklady (postoj pro)", default=list, blank=True)
    kind = models.CharField(
        "druh", max_length=10, choices=NarrativeKind.choices, default=NarrativeKind.NARRATIVE
    )
    counter_examples = models.JSONField("příklady (postoj proti)", default=list, blank=True)
    stance_guide = models.JSONField("vodítko k postojům", default=dict, blank=True)
    cues = models.JSONField("signální slova", default=list, blank=True)
    attribution = models.TextField("zdroj a atribuce", blank=True)
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


class LabelQuerySet(models.QuerySet):
    def current(self):
        """Platné štítky: dokument nebyl posouzen podle novější verze narativu.

        Archiv se po úpravě narativu automaticky znovu neanotuje, takže štítek starší verze
        platí, dokud dokument neposoudí novější verze (ruční zpětná anotace); pak zůstává
        jen jako historie.
        """
        newer = LabelDecision.objects.filter(
            document=OuterRef("document"),
            narrative=OuterRef("narrative"),
            narrative_version__version__gt=OuterRef("narrative_version__version"),
            present__isnull=False,
        )
        return self.exclude(Exists(newer))


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
    details = models.JSONField(
        "podrobnosti",
        default=dict,
        blank=True,
        help_text="Proč model štítek přidělil: podobnost, signální slova, skóre postojů.",
    )
    annotator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="anotátor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("vytvořen", auto_now_add=True)

    objects = LabelQuerySet.as_manager()

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

    description = models.TextField(
        "popis shluku", help_text="Tvrzení, kterým shluk začal (formulace modelu)."
    )
    centroid = VectorField("střed shluku", dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    tracked_since = models.DateTimeField(
        "sledován od",
        null=True,
        blank=True,
        help_text="Kdy shluk splnil pravidlo přidání a vznikl z něj narativ ve stavu kandidát.",
    )
    keywords = models.JSONField("klíčová slova", default=list, blank=True)
    example_documents = models.ManyToManyField(
        Document, verbose_name="příklady", blank=True, related_name="+"
    )
    nearest_narratives = models.ManyToManyField(
        Narrative, verbose_name="nejbližší narativy", blank=True, related_name="+"
    )
    n_documents = models.PositiveIntegerField("dokumentů v okně", default=0)
    n_authors = models.PositiveIntegerField("nezávislých autorů v okně", default=0)
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


class LabelDecision(models.Model):
    """Решение модели по паре «документ × версия нарратива» (README §4, шаг 7).

    Кандидатов отбирают эмбеддинги и сигнальные слова; решение (есть ли нарратив и какая
    позиция) принимает LLM. Хранятся и отрицательные решения: они нужны для валидации
    (precision/recall) и чтобы не платить за повторный вызов. Пустое `present` — кандидат
    ждёт модель.
    """

    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="label_decisions"
    )
    narrative = models.ForeignKey(
        Narrative, verbose_name="narativ", on_delete=models.CASCADE, related_name="decisions"
    )
    narrative_version = models.ForeignKey(
        NarrativeVersion,
        verbose_name="verze kódové knihy",
        on_delete=models.CASCADE,
        related_name="decisions",
    )
    text_hash = models.CharField("hash textu", max_length=64)
    similarity = models.FloatField("podobnost s narativem")
    cues = models.JSONField("nalezená signální slova", default=list, blank=True)
    model_name = models.CharField("model", max_length=100, blank=True)
    prompt_version = models.CharField("verze zadání", max_length=20, blank=True)
    present = models.BooleanField("narativ přítomen", null=True, blank=True)
    stance = models.CharField("postoj", max_length=10, choices=Stance.choices, blank=True)
    confidence = models.FloatField("jistota", null=True, blank=True)
    evidence = models.TextField("doklad (citace z textu)", blank=True)
    carrier = models.CharField(
        "kdo narativ v textu nese",
        max_length=200,
        blank=True,
        help_text="Autor textu, nebo citovaná osoba či organizace (např. politik v článku).",
    )
    reason = models.TextField("zdůvodnění modelu", blank=True)
    cost_usd = models.FloatField("cena (USD)", null=True, blank=True)
    error = models.TextField("chyba", blank=True)
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)
    decided_at = models.DateTimeField("rozhodnuto", null=True, blank=True)

    class Meta:
        verbose_name = "rozhodnutí modelu"
        verbose_name_plural = "rozhodnutí modelu"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "narrative_version", "text_hash"],
                name="label_decision_unique",
            ),
        ]
        indexes = [models.Index(fields=["present", "decided_at"])]

    def __str__(self):
        state = "čeká" if self.present is None else ("ano" if self.present else "ne")
        return f"{self.document_id} × {self.narrative_version}: {state}"


class Screening(models.Model):
    """Kontrola relevantního dokumentu modelem: hledají se tvrzení, která kódová kniha nezná.

    Probíhá v tomtéž volání modelu jako anotace známých narativů. Prázdné `checked_at` —
    dokument čeká na model.
    """

    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="screenings"
    )
    text_hash = models.CharField("hash textu", max_length=64)
    model_name = models.CharField("model", max_length=100, blank=True)
    prompt_version = models.CharField("verze zadání", max_length=20, blank=True)
    n_claims = models.PositiveSmallIntegerField("nových tvrzení", default=0)
    cost_usd = models.FloatField("cena (USD)", null=True, blank=True)
    error = models.TextField("chyba", blank=True)
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)
    checked_at = models.DateTimeField("zkontrolováno", null=True, blank=True)

    class Meta:
        verbose_name = "kontrola nových tvrzení"
        verbose_name_plural = "kontroly nových tvrzení"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["document", "text_hash"], name="screening_unique_text"),
        ]
        indexes = [models.Index(fields=["checked_at"])]

    def __str__(self):
        return f"{self.document_id}: {'čeká' if self.checked_at is None else self.n_claims}"


class NovelClaim(models.Model):
    """Tvrzení z dokumentu, které model nenašel mezi známými narativy.

    Podobná tvrzení se skládají do shluku (NarrativeCandidate); tvrzení blízké existujícímu
    narativu se místo toho posoudí jako jeho kandidát.
    """

    screening = models.ForeignKey(
        Screening, verbose_name="kontrola", on_delete=models.CASCADE, related_name="claims"
    )
    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="novel_claims"
    )
    claim = models.TextField("tvrzení")
    stance = models.CharField("postoj dokumentu", max_length=10, choices=Stance.choices)
    confidence = models.FloatField("jistota", default=0)
    carrier = models.CharField("kdo tvrzení v textu nese", max_length=200, blank=True)
    evidence = models.TextField("doklad (citace z textu)", blank=True)
    embedding = VectorField("embedding", dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    candidate = models.ForeignKey(
        NarrativeCandidate,
        verbose_name="shluk",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claims",
    )
    matched_narrative = models.ForeignKey(
        Narrative,
        verbose_name="odpovídá narativu",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Tvrzení je blízké už sledovanému narativu: dokument se posoudí podle něj.",
    )
    similarity = models.FloatField("podobnost", null=True, blank=True)
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)

    class Meta:
        verbose_name = "nové tvrzení"
        verbose_name_plural = "nová tvrzení"
        ordering = ["-created_at"]

    def __str__(self):
        return self.claim[:80]
