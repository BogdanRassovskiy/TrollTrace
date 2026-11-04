"""Валидация (README §4, шаг 9): ручная разметка выборки, согласие разметчиков, сравнение
с автоматикой. Выборка фиксирует версии нарративов: разметчики и модель оцениваются по одному
и тому же определению.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel
from apps.documents.models import Document
from apps.narratives.models import NarrativeVersion


class Sample(TimeStampedModel):
    """Выборка для ручной разметки."""

    class Kind(models.TextChoices):
        NARRATIVES = "narratives", "Narativy a postoje"
        REUSE = "reuse", "Převzetí textu"

    class Status(models.TextChoices):
        OPEN = "open", "Probíhá anotace"
        CLOSED = "closed", "Uzavřen"

    name = models.CharField("název", max_length=200)
    kind = models.CharField("druh", max_length=12, choices=Kind.choices)
    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.OPEN)
    description = models.TextField("popis", blank=True)
    narrative_versions = models.ManyToManyField(
        NarrativeVersion,
        verbose_name="verze narativů",
        blank=True,
        related_name="+",
        help_text="Anotuje se podle těchto verzí; model se hodnotí podle rozhodnutí ke "
        "stejným verzím.",
    )
    hide_source = models.BooleanField(
        "skrýt zdroj a autora",
        default=True,
        help_text="Anotátor vidí jen titulek, datum a text: název média nebo politika by "
        "mohl ovlivnit posouzení.",
    )
    params = models.JSONField(
        "parametry výběru",
        default=dict,
        blank=True,
        help_text="Velikosti vrstev výběru, velikost populace, semínko náhody, prahy v době "
        "výběru.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="vytvořil",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "validační vzorek"
        verbose_name_plural = "validační vzorky"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class SampleItem(models.Model):
    """Элемент выборки: документ (нарративы) или пара документов (заимствования)."""

    sample = models.ForeignKey(
        Sample, verbose_name="vzorek", on_delete=models.CASCADE, related_name="items"
    )
    position = models.PositiveIntegerField("pořadí")
    stratum = models.CharField("vrstva výběru", max_length=20)
    weight = models.FloatField(
        "váha",
        null=True,
        blank=True,
        help_text="Velikost vrstvy v populaci / počet vybraných: převádí výsledky na populaci.",
    )
    document = models.ForeignKey(
        Document,
        verbose_name="dokument",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    source_document = models.ForeignKey(
        Document,
        verbose_name="možný originál",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    target_document = models.ForeignKey(
        Document,
        verbose_name="možné převzetí",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    similarity = models.FloatField("podobnost při výběru", null=True, blank=True)

    class Meta:
        verbose_name = "položka vzorku"
        verbose_name_plural = "položky vzorku"
        ordering = ["sample", "position"]
        constraints = [
            models.UniqueConstraint(fields=["sample", "position"], name="sample_item_position"),
            models.CheckConstraint(
                condition=Q(document__isnull=False)
                | Q(source_document__isnull=False, target_document__isnull=False),
                name="sample_item_has_documents",
            ),
        ]

    def __str__(self):
        return f"{self.sample} #{self.position}"


class Value(models.TextChoices):
    """Значение разметки по нарративу: присутствие и позиция одной категорией."""

    ABSENT = "absent", "Narativ není"
    PRO = "pro", "Pro"
    CONTRA = "contra", "Proti"
    NEUTRAL = "neutral", "Neutrálně"


class ReuseVerdict(models.TextChoices):
    VERBATIM = "verbatim", "Doslovné převzetí"
    PARAPHRASE = "paraphrase", "Parafráze (převyprávění téhož textu)"
    NONE = "none", "Není převzetí (jen stejné téma nebo událost)"


class Review(models.Model):
    """Работа одного разметчика над одним элементом."""

    item = models.ForeignKey(
        SampleItem, verbose_name="položka", on_delete=models.CASCADE, related_name="reviews"
    )
    annotator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="anotátor",
        on_delete=models.PROTECT,
        related_name="validation_reviews",
    )
    unclear = models.BooleanField(
        "nelze posoudit",
        default=False,
        help_text="Text je nečitelný, cizojazyčný nebo jinak neposouditelný; do výpočtů nejde.",
    )
    reuse_verdict = models.CharField(
        "převzetí", max_length=12, choices=ReuseVerdict.choices, blank=True
    )
    note = models.TextField("poznámka", blank=True)
    created_at = models.DateTimeField("vytvořeno", auto_now_add=True)
    updated_at = models.DateTimeField("změněno", auto_now=True)

    class Meta:
        verbose_name = "anotace položky"
        verbose_name_plural = "anotace položek"
        constraints = [
            models.UniqueConstraint(fields=["item", "annotator"], name="review_unique_annotator"),
        ]

    def __str__(self):
        return f"{self.item} — {self.annotator}"


class Annotation(models.Model):
    """Значение разметчика для одного нарратива в документе."""

    review = models.ForeignKey(
        Review,
        verbose_name="anotace položky",
        on_delete=models.CASCADE,
        related_name="annotations",
    )
    narrative_version = models.ForeignKey(
        NarrativeVersion,
        verbose_name="verze narativu",
        on_delete=models.PROTECT,
        related_name="+",
    )
    value = models.CharField("hodnota", max_length=10, choices=Value.choices)

    class Meta:
        verbose_name = "anotace narativu"
        verbose_name_plural = "anotace narativů"
        constraints = [
            models.UniqueConstraint(
                fields=["review", "narrative_version"], name="annotation_unique_narrative"
            ),
        ]

    def __str__(self):
        return f"{self.review}: {self.narrative_version} = {self.value}"


class Adjudication(models.Model):
    """Решение исследователя там, где разметчики разошлись: становится эталоном."""

    item = models.ForeignKey(
        SampleItem,
        verbose_name="položka",
        on_delete=models.CASCADE,
        related_name="adjudications",
    )
    narrative_version = models.ForeignKey(
        NarrativeVersion,
        verbose_name="verze narativu",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Prázdné u vzorku převzetí.",
    )
    value = models.CharField("referenční hodnota", max_length=12)
    note = models.TextField("zdůvodnění", blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="rozhodl",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField("kdy", auto_now=True)

    class Meta:
        verbose_name = "rozhodnutí sporu"
        verbose_name_plural = "rozhodnutí sporů"
        constraints = [
            models.UniqueConstraint(
                fields=["item", "narrative_version"],
                name="adjudication_unique",
                nulls_distinct=False,
            ),
        ]

    def __str__(self):
        return f"{self.item}: {self.value}"


class Report(models.Model):
    """Сохранённый результат валидации (согласие, precision/recall) для главы о методологии."""

    sample = models.ForeignKey(
        Sample, verbose_name="vzorek", on_delete=models.CASCADE, related_name="reports"
    )
    model_name = models.CharField(
        "model", max_length=100, blank=True, help_text="Prázdné — všechna rozhodnutí modelu."
    )
    metrics = models.JSONField("výsledky", default=dict)
    note = models.TextField("poznámka", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="uložil",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("uloženo", auto_now_add=True)

    class Meta:
        verbose_name = "validační zpráva"
        verbose_name_plural = "validační zprávy"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.sample} ({self.created_at:%Y-%m-%d %H:%M})"
