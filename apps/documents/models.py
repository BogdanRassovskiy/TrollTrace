"""Документы, их версии, оценка качества и связи заимствований (README §4.1, §4.3, §9)."""

import zlib

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models, transaction
from django.db.models import F, Q
from pgvector.django import HnswIndex, VectorField

from apps.actors.models import Actor, Source

from .utils import normalize_url, text_hash

# multilingual-e5-base и LaBSE дают векторы размерности 768 (README §4, шаг 4).
# Смена модели с другой размерностью требует миграции и пересчёта эмбеддингов.
EMBEDDING_DIMENSIONS = 768


class Document(models.Model):
    """Пост, статья или выступление. Хранится полный текст (README §6)."""

    class DateSource(models.TextChoices):
        PAGE = "page", "Stránka"
        FEED = "feed", "RSS / API"
        METADATA = "meta", "Metadata"
        NONE = "none", "Nezjištěno"

    class Status(models.TextChoices):
        ACTIVE = "active", "Dostupný u zdroje"
        DELETED = "deleted", "Smazán u zdroje"

    actor = models.ForeignKey(
        Actor, verbose_name="aktér", on_delete=models.PROTECT, related_name="documents"
    )
    source = models.ForeignKey(
        Source, verbose_name="zdroj", on_delete=models.PROTECT, related_name="documents"
    )
    external_id = models.CharField(
        "externí ID",
        max_length=200,
        blank=True,
        help_text="ID zprávy Telegramu, stenoprotokolu PSP…",
    )
    url = models.URLField("původní URL", max_length=2000)
    url_normalized = models.CharField("normalizované URL", max_length=2000, editable=False)

    title = models.TextField("titulek", blank=True)
    text = models.TextField("text (poslední verze)", blank=True)
    text_hash = models.CharField("hash textu", max_length=64, editable=False, db_index=True)
    rubric = models.CharField("rubrika", max_length=200, blank=True)
    in_scope = models.BooleanField(
        "ve sledovaných rubrikách",
        null=True,
        db_index=True,
        help_text="Ne — rubrika mimo výzkum (sport, celebrity…), dokument není ve jmenovateli. "
        "Prázdné — rubriku se nepodařilo zjistit.",
    )
    language = models.CharField("jazyk", max_length=8, blank=True)

    # Два времени (README §4.1): дата публикации не подменяется временем сбора.
    published_at = models.DateTimeField("publikováno", null=True, blank=True)
    date_source = models.CharField(
        "zdroj data", max_length=4, choices=DateSource.choices, default=DateSource.NONE
    )
    collected_at = models.DateTimeField("sebráno")
    last_checked_at = models.DateTimeField("poslední kontrola", null=True, blank=True)

    status = models.CharField("stav", max_length=10, choices=Status.choices, default=Status.ACTIVE)
    deleted_at = models.DateTimeField("smazán u zdroje", null=True, blank=True)

    is_relevant = models.BooleanField(
        "relevantní", null=True, help_text="Prázdné — filtr zatím nebyl použit."
    )
    engagement = models.JSONField(
        "metriky odezvy", default=dict, blank=True, help_text="Zobrazení, reakce, sdílení."
    )
    meta = models.JSONField(
        "metadata zdroje",
        default=dict,
        blank=True,
        help_text="Rubrika kanálu, přeposlání v Telegramu, řečník a stránka stenoprotokolu…",
    )
    # Воронка обработки (README §4, шаги 3–6), apps.processing.
    processed_at = models.DateTimeField(
        "zpracováno",
        null=True,
        blank=True,
        db_index=True,
        help_text="Prázdné — dokument čeká na zpracování (relevance, embedding, převzetí).",
    )
    relevance_score = models.FloatField(
        "skóre relevance",
        null=True,
        blank=True,
        help_text="Nejvyšší centrovaná kosinová podobnost s tématy a narativy.",
    )
    relevance_details = models.JSONField(
        "podrobnosti relevance",
        default=dict,
        blank=True,
        help_text="Důvod rozhodnutí, nalezená klíčová slova, nejbližší téma a narativ.",
    )
    embedding = VectorField("embedding", dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    embedding_text_hash = models.CharField(
        "hash textu embeddingu",
        max_length=64,
        blank=True,
        editable=False,
        help_text="Embedding se počítá jednou pro každé znění textu a model.",
    )
    minhash = models.BinaryField("MinHash podpis", null=True, blank=True)
    minhash_bands = ArrayField(
        models.BigIntegerField(),
        verbose_name="LSH pásma",
        null=True,
        blank=True,
        editable=False,
        help_text="Hashe pásem MinHash podpisu pro hledání kandidátů na doslovné převzetí.",
    )

    class Meta:
        verbose_name = "dokument"
        verbose_name_plural = "dokumenty"
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "url_normalized"], name="document_unique_url_per_source"
            ),
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="document_unique_external_id",
            ),
            models.CheckConstraint(
                condition=Q(status="deleted", deleted_at__isnull=False)
                | Q(status="active", deleted_at__isnull=True),
                name="document_deleted_at_matches_status",
            ),
        ]
        indexes = [
            models.Index(fields=["actor", "published_at"]),
            models.Index(fields=["source", "collected_at"]),
            GinIndex(fields=["minhash_bands"], name="document_minhash_bands_gin"),
            HnswIndex(
                name="document_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self):
        return self.title[:80] or self.url

    def save(self, *args, **kwargs):
        # Внешний ID означает, что фрагмент URL адресует отдельный документ (#r3 в стенограмме).
        self.url_normalized = normalize_url(self.url, keep_fragment=bool(self.external_id))
        self.text_hash = text_hash(self.text)
        super().save(*args, **kwargs)

    def record_version(self, *, text: str, title: str = "", raw_html: str = ""):
        """Сохраняет текст как новую версию, если он изменился. Старые версии не затираются.

        Возвращает созданную версию или None, если текст тот же.
        """
        new_hash = text_hash(text)
        with transaction.atomic():
            latest = self.versions.select_for_update().order_by("-version").first()
            if latest is not None and latest.text_hash == new_hash:
                return None
            version = DocumentVersion(
                document=self,
                version=(latest.version + 1) if latest else 1,
                title=title,
                text=text,
            )
            version.raw_html = raw_html
            version.save()
            self.title, self.text = title or self.title, text
            if latest is not None:
                self.processed_at = None  # новая редакция текста проходит воронку заново
            self.save()
        return version


class DocumentVersion(models.Model):
    """Версия документа. Правки и удаления сами по себе могут быть данными (README §4.1)."""

    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField("verze")
    title = models.TextField("titulek", blank=True)
    text = models.TextField("text", blank=True)
    text_hash = models.CharField("hash textu", max_length=64, editable=False)
    raw_html_compressed = models.BinaryField(
        "surové HTML (zlib)",
        null=True,
        blank=True,
        help_text="Pro opakované zpracování bez stahování.",
    )
    detected_at = models.DateTimeField("zjištěno", auto_now_add=True)

    class Meta:
        verbose_name = "verze dokumentu"
        verbose_name_plural = "verze dokumentů"
        ordering = ["document", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version"], name="document_version_unique_number"
            ),
        ]

    def __str__(self):
        return f"{self.document_id} v{self.version}"

    def save(self, *args, **kwargs):
        self.text_hash = text_hash(self.text)
        super().save(*args, **kwargs)

    @property
    def raw_html(self) -> str:
        if not self.raw_html_compressed:
            return ""
        return zlib.decompress(bytes(self.raw_html_compressed)).decode("utf-8")

    @raw_html.setter
    def raw_html(self, value: str):
        self.raw_html_compressed = zlib.compress(value.encode("utf-8"), 9) if value else None


class DocumentQuality(models.Model):
    """Оценка качества сбора 1–5 (README §4.3): сначала флаги, оценка выводится из них.

    Оценка говорит о технической корректности сбора и извлечения, не о содержании.
    История пересчётов сохраняется; актуальна одна запись на документ (is_current).
    """

    class IncidentLevel(models.TextChoices):
        NONE = "", "Ne"
        WARNING = "warning", "Varování"
        CRITICAL = "critical", "Kritické"

    document = models.ForeignKey(
        Document, verbose_name="dokument", on_delete=models.CASCADE, related_name="quality_scores"
    )
    score = models.PositiveSmallIntegerField("hodnocení")
    quality_rules_version = models.CharField("verze pravidel", max_length=20)
    computed_at = models.DateTimeField("vypočteno", auto_now_add=True)
    is_current = models.BooleanField("aktuální", default=True)

    has_title = models.BooleanField("má titulek")
    has_published_at = models.BooleanField("má datum publikace")
    date_plausible = models.BooleanField("datum je věrohodné")
    text_ok = models.BooleanField("délka textu v normě")
    text_short = models.BooleanField("text je krátký")
    text_empty = models.BooleanField("text je prázdný")
    lang_ok = models.BooleanField("jazyk odpovídá")
    boilerplate_dup = models.BooleanField("zástupný text u různých URL")
    blocked_page = models.BooleanField("captcha / paywall / souhlas / chyba")
    selectors_ok = models.BooleanField(
        "selektory nalezeny", null=True, help_text="Prázdné — vlastní selektory se nepoužívají."
    )
    extraction_ratio_ok = models.BooleanField("poměr text/HTML v normě")
    incident_level = models.CharField(
        "otevřený incident zdroje",
        max_length=10,
        choices=IncidentLevel.choices,
        blank=True,
        default=IncidentLevel.NONE,
    )

    class Meta:
        verbose_name = "hodnocení kvality"
        verbose_name_plural = "hodnocení kvality"
        ordering = ["document", "-computed_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(score__gte=1, score__lte=5), name="document_quality_score_1_5"
            ),
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(is_current=True),
                name="document_quality_one_current",
            ),
        ]
        indexes = [
            models.Index(fields=["score"], condition=Q(is_current=True), name="dq_current_score")
        ]

    def __str__(self):
        return f"{self.document_id}: {self.score} ({self.quality_rules_version})"

    def save(self, *args, **kwargs):
        """Новая актуальная оценка переводит прежнюю в историю."""
        with transaction.atomic():
            if self.is_current and self._state.adding:
                DocumentQuality.objects.filter(document=self.document, is_current=True).update(
                    is_current=False
                )
            super().save(*args, **kwargs)


class ReuseLink(models.Model):
    """Пара документов с заимствованием: от более раннего (source) к более позднему (target)."""

    class Type(models.TextChoices):
        VERBATIM = "verbatim", "Doslovně"
        PARAPHRASE = "paraphrase", "Parafráze"
        REPOST = "repost", "Sdílení"
        QUOTE = "quote", "Citace"

    class Method(models.TextChoices):
        MINHASH = "minhash", "MinHash / LSH"
        KNN = "knn", "kNN podle embeddingů"
        MANUAL = "manual", "Ručně"

    source_document = models.ForeignKey(
        Document, verbose_name="originál", on_delete=models.CASCADE, related_name="reused_by"
    )
    target_document = models.ForeignKey(
        Document, verbose_name="převzetí", on_delete=models.CASCADE, related_name="reuses"
    )
    link_type = models.CharField("typ", max_length=12, choices=Type.choices)
    method = models.CharField("metoda", max_length=10, choices=Method.choices)
    similarity = models.FloatField("podobnost")
    is_confirmed = models.BooleanField(
        "ručně potvrzeno", null=True, help_text="Prázdné — nekontrolováno."
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="kdo zkontroloval",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    confirmed_at = models.DateTimeField("kdy zkontrolováno", null=True, blank=True)
    created_at = models.DateTimeField("nalezeno", auto_now_add=True)

    class Meta:
        verbose_name = "převzetí"
        verbose_name_plural = "převzetí"
        constraints = [
            models.UniqueConstraint(
                fields=["source_document", "target_document", "link_type"],
                name="reuse_link_unique_pair_type",
            ),
            models.CheckConstraint(
                condition=~Q(source_document=F("target_document")), name="reuse_link_not_self"
            ),
            models.CheckConstraint(
                condition=Q(similarity__gte=0, similarity__lte=1), name="reuse_link_similarity_0_1"
            ),
        ]

    def __str__(self):
        return f"{self.source_document_id} → {self.target_document_id} ({self.link_type})"
