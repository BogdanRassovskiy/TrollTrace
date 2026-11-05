"""Сохранение собранного документа по правилам README §4.1.

- Дедупликация по нормализованному URL (а для постов и выступлений — по внешнему ID).
- Тот же URL с другим текстом → новая версия, старая не затирается.
- Новый URL с тем же текстом нормальной длины в том же источнике — та же статья по другому
  адресу: не дублируем. Короткие одинаковые тексты (заглушки, капча) сохраняются — по ним
  мониторинг (Этап 3.1) распознаёт поломки.
"""

import datetime
import enum
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.actors.models import Actor, Source
from apps.documents.models import Document
from apps.documents.utils import normalize_url, text_hash

from . import quality
from .parsers import PARSER_META_KEYS
from .scope import rubric_in_scope

MAX_DUPLICATE_URLS = 10


class Outcome(enum.StrEnum):
    NEW = "new"
    VERSION = "versions"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicates"


@dataclass
class Item:
    """Документ, как его увидел коллектор."""

    url: str
    text: str
    title: str = ""
    raw_html: str = ""
    published_at: datetime.datetime | None = None
    date_source: str = Document.DateSource.NONE
    rubric: str = ""
    language: str = ""
    external_id: str = ""
    engagement: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


def save_item(source: Source, actor: Actor, item: Item) -> tuple[Outcome, Document | None]:
    url_normalized = normalize_url(item.url, keep_fragment=bool(item.external_id))
    now = timezone.now()
    with transaction.atomic():
        existing = Document.objects.select_for_update().filter(source=source)
        doc = existing.filter(url_normalized=url_normalized).first()
        if doc is None and item.external_id:
            doc = existing.filter(external_id=item.external_id).first()

        if doc is not None:
            version = doc.record_version(text=item.text, title=item.title, raw_html=item.raw_html)
            doc.last_checked_at = now
            if item.engagement:
                doc.engagement = item.engagement
            fields = ["last_checked_at", "engagement"]
            if item.meta.get("parser"):  # результат парсера сайта — по последнему извлечению
                kept = {k: v for k, v in doc.meta.items() if k not in PARSER_META_KEYS}
                doc.meta = kept | {k: v for k, v in item.meta.items() if k in PARSER_META_KEYS}
                fields.append("meta")
            if item.rubric and not doc.rubric:
                doc.rubric = item.rubric[:200]
                doc.in_scope = rubric_in_scope(source, doc.rubric)
                fields += ["rubric", "in_scope"]
            doc.save(update_fields=fields)
            if version:
                quality.assess(doc, raw_html=item.raw_html)
            return (Outcome.VERSION if version else Outcome.UNCHANGED), doc

        original = (
            Document.objects.filter(source=source, actor=actor, text_hash=text_hash(item.text))
            .select_for_update()
            .first()
            if len(item.text) >= source.min_text_length
            else None
        )
        if original is not None:
            # Тот же текст под другим URL — признак заглушки (куки, капча, ошибка), README §4.2.
            duplicates = original.meta.get("duplicate_urls", [])
            if item.url not in duplicates and len(duplicates) < MAX_DUPLICATE_URLS:
                original.meta = {**original.meta, "duplicate_urls": [*duplicates, item.url]}
                original.save(update_fields=["meta"])
                if len(duplicates) + 1 == quality.BOILERPLATE_MIN_DUPLICATES:
                    quality.assess(original)  # с этого повтора текст считается заглушкой
            return Outcome.DUPLICATE, None

        doc = Document.objects.create(
            actor=actor,
            source=source,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            text=item.text,
            rubric=item.rubric[:200],
            in_scope=rubric_in_scope(source, item.rubric[:200]),
            language=item.language or quality.detect_language(item.text),
            published_at=item.published_at,
            date_source=item.date_source,
            collected_at=now,
            engagement=item.engagement,
            meta=item.meta,
        )
        doc.record_version(text=item.text, title=item.title, raw_html=item.raw_html)
        quality.assess(doc, raw_html=item.raw_html)
        return Outcome.NEW, doc
