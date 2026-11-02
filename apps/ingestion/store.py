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
            doc.save(update_fields=["last_checked_at", "engagement"])
            return (Outcome.VERSION if version else Outcome.UNCHANGED), doc

        if (
            len(item.text) >= source.min_text_length
            and Document.objects.filter(
                source=source, actor=actor, text_hash=text_hash(item.text)
            ).exists()
        ):
            return Outcome.DUPLICATE, None

        doc = Document.objects.create(
            actor=actor,
            source=source,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            text=item.text,
            rubric=item.rubric[:200],
            language=item.language,
            published_at=item.published_at,
            date_source=item.date_source,
            collected_at=now,
            engagement=item.engagement,
            meta=item.meta,
        )
        doc.record_version(text=item.text, title=item.title, raw_html=item.raw_html)
        return Outcome.NEW, doc
