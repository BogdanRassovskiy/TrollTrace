"""Документы, их версии, оценка качества и связи заимствований (README §4.1, §4.3, §9)."""

import zlib

from django.conf import settings
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
        PAGE = "page", "Страница"
        FEED = "feed", "RSS / API"
        METADATA = "meta", "Метаданные"
        NONE = "none", "Не извлечена"

    class Status(models.TextChoices):
        ACTIVE = "active", "Доступен у источника"
        DELETED = "deleted", "Удалён у источника"

    actor = models.ForeignKey(
        Actor, verbose_name="актор", on_delete=models.PROTECT, related_name="documents"
    )
    source = models.ForeignKey(
        Source, verbose_name="источник", on_delete=models.PROTECT, related_name="documents"
    )
    external_id = models.CharField(
        "внешний ID",
        max_length=200,
        blank=True,
        help_text="ID сообщения Telegram, стенограммы PSP…",
    )
    url = models.URLField("исходный URL", max_length=2000)
    url_normalized = models.CharField("нормализованный URL", max_length=2000, editable=False)

    title = models.TextField("заголовок", blank=True)
    text = models.TextField("текст (последняя версия)", blank=True)
    text_hash = models.CharField("хэш текста", max_length=64, editable=False, db_index=True)
    rubric = models.CharField("рубрика", max_length=200, blank=True)
    language = models.CharField("язык", max_length=8, blank=True)

    # Два времени (README §4.1): дата публикации не подменяется временем сбора.
    published_at = models.DateTimeField("опубликовано", null=True, blank=True)
    date_source = models.CharField(
        "откуда дата", max_length=4, choices=DateSource.choices, default=DateSource.NONE
    )
    collected_at = models.DateTimeField("собрано")
    last_checked_at = models.DateTimeField("последняя перепроверка", null=True, blank=True)

    status = models.CharField(
        "статус", max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    deleted_at = models.DateTimeField("удалён у источника", null=True, blank=True)

    is_relevant = models.BooleanField(
        "релевантен", null=True, help_text="Пусто — фильтр ещё не применялся."
    )
    engagement = models.JSONField(
        "метрики отклика", default=dict, blank=True, help_text="Просмотры, реакции, репосты."
    )
    embedding = VectorField("эмбеддинг", dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    minhash = models.BinaryField("MinHash-подпись", null=True, blank=True)

    class Meta:
        verbose_name = "документ"
        verbose_name_plural = "документы"
        ordering = ["-published_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "url_normalized"], name="document_unique_url_per_source"
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
        self.url_normalized = normalize_url(self.url)
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
            self.save()
        return version


class DocumentVersion(models.Model):
    """Версия документа. Правки и удаления сами по себе могут быть данными (README §4.1)."""

    document = models.ForeignKey(
        Document, verbose_name="документ", on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField("версия")
    title = models.TextField("заголовок", blank=True)
    text = models.TextField("текст", blank=True)
    text_hash = models.CharField("хэш текста", max_length=64, editable=False)
    raw_html_compressed = models.BinaryField(
        "сырой HTML (zlib)", null=True, blank=True, help_text="Для перепарсинга без скачивания."
    )
    detected_at = models.DateTimeField("обнаружена", auto_now_add=True)

    class Meta:
        verbose_name = "версия документа"
        verbose_name_plural = "версии документов"
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
        NONE = "", "Нет"
        WARNING = "warning", "Предупреждение"
        CRITICAL = "critical", "Критично"

    document = models.ForeignKey(
        Document, verbose_name="документ", on_delete=models.CASCADE, related_name="quality_scores"
    )
    score = models.PositiveSmallIntegerField("оценка")
    quality_rules_version = models.CharField("версия правил", max_length=20)
    computed_at = models.DateTimeField("рассчитано", auto_now_add=True)
    is_current = models.BooleanField("актуальная", default=True)

    has_title = models.BooleanField("есть заголовок")
    has_published_at = models.BooleanField("есть дата публикации")
    date_plausible = models.BooleanField("дата правдоподобна")
    text_ok = models.BooleanField("длина текста в норме")
    text_short = models.BooleanField("текст короткий")
    text_empty = models.BooleanField("текст пустой")
    lang_ok = models.BooleanField("язык совпадает")
    boilerplate_dup = models.BooleanField("текст-заглушка у разных URL")
    blocked_page = models.BooleanField("капча / paywall / согласие / ошибка")
    selectors_ok = models.BooleanField(
        "селекторы найдены", null=True, help_text="Пусто — собственные селекторы не используются."
    )
    extraction_ratio_ok = models.BooleanField("соотношение текст/HTML в норме")
    incident_level = models.CharField(
        "открытый инцидент источника",
        max_length=10,
        choices=IncidentLevel.choices,
        blank=True,
        default=IncidentLevel.NONE,
    )

    class Meta:
        verbose_name = "оценка качества"
        verbose_name_plural = "оценки качества"
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
        VERBATIM = "verbatim", "Дословно"
        PARAPHRASE = "paraphrase", "Пересказ"
        REPOST = "repost", "Репост"
        QUOTE = "quote", "Цитата"

    class Method(models.TextChoices):
        MINHASH = "minhash", "MinHash / LSH"
        KNN = "knn", "kNN по эмбеддингам"
        MANUAL = "manual", "Вручную"

    source_document = models.ForeignKey(
        Document, verbose_name="оригинал", on_delete=models.CASCADE, related_name="reused_by"
    )
    target_document = models.ForeignKey(
        Document, verbose_name="заимствование", on_delete=models.CASCADE, related_name="reuses"
    )
    link_type = models.CharField("тип", max_length=12, choices=Type.choices)
    method = models.CharField("метод", max_length=10, choices=Method.choices)
    similarity = models.FloatField("сходство")
    is_confirmed = models.BooleanField(
        "подтверждено вручную", null=True, help_text="Пусто — не проверялось."
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто проверил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    confirmed_at = models.DateTimeField("когда проверено", null=True, blank=True)
    created_at = models.DateTimeField("найдено", auto_now_add=True)

    class Meta:
        verbose_name = "заимствование"
        verbose_name_plural = "заимствования"
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
