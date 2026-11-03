"""Поиск заимствований (README §4, шаги 5–6): из слоя-источника (A, Pravda) в другие слои.

Связь всегда идёт от более раннего документа слоя-источника к более позднему
документу другого слоя. Порядок поступления не важен: новый документ ищет пары
в обе стороны по времени, в пределах окна reuse-window-days.
"""

import datetime

import numpy as np
from django.db import connection
from django.db.models import Q, QuerySet
from django.db.models.functions import Coalesce
from pgvector.django import CosineDistance

from apps.actors.models import ANALYSIS_LAYERS
from apps.documents.models import Document, ReuseLink
from apps.panel.defaults import get_setting

from . import minhash
from .embeddings import centered

KNN_CANDIDATES = 20  # ближайших по «сырому» косинусу; дальше — точная центрированная оценка
AUTO_METHODS = [ReuseLink.Method.MINHASH, ReuseLink.Method.KNN]


def source_layers() -> list[str]:
    return list(get_setting("reuse-source-layers"))


def moment(document: Document) -> datetime.datetime:
    return document.published_at or document.collected_at


def counterparts(document: Document, layers: list[str] | None = None) -> tuple[QuerySet, bool]:
    """Документы, с которыми возможна связь, и роль документа (True — он оригинал)."""
    layers = source_layers() if layers is None else layers
    window = datetime.timedelta(days=int(get_setting("reuse-window-days")))
    when = moment(document)
    queryset = (
        Document.objects.annotate(moment=Coalesce("published_at", "collected_at"))
        .exclude(pk=document.pk)
        .exclude(actor_id=document.actor_id)
        # Слой I говорит о нарративах, а не транслирует их — в заимствованиях не участвует.
        .filter(source__layer__in=ANALYSIS_LAYERS)
    )
    if document.source.layer not in ANALYSIS_LAYERS:
        return queryset.none(), False
    if document.source.layer in layers:
        return (
            queryset.exclude(source__layer__in=layers).filter(
                moment__gte=when, moment__lte=when + window
            ),
            True,
        )
    return (
        queryset.filter(source__layer__in=layers, moment__lte=when, moment__gte=when - window),
        False,
    )


def _pair(document: Document, other: Document, is_source: bool) -> tuple[Document, Document]:
    return (document, other) if is_source else (other, document)


def find_verbatim(document: Document, layers=None, shingle_size=None) -> list[dict]:
    """Кандидаты LSH → проверка по подписи. Сходство = max(Jaccard, доля заимствованного)."""
    if not document.minhash_bands or not document.minhash:
        return []
    threshold = float(get_setting("reuse-verbatim-jaccard"))
    size = shingle_size or int(get_setting("minhash-shingle-size"))
    queryset, is_source = counterparts(document, layers)
    mine = minhash.from_bytes(document.minhash)
    my_size = len(minhash.shingles(document.text, size))
    found = []
    candidates = queryset.filter(minhash_bands__overlap=document.minhash_bands).only(
        "id", "text", "minhash", "published_at", "collected_at", "actor_id", "source_id"
    )
    for other in candidates:
        estimate = minhash.jaccard(mine, minhash.from_bytes(other.minhash))
        other_size = len(minhash.shingles(other.text, size))
        source, target = _pair(document, other, is_source)
        source_size, target_size = (my_size, other_size) if is_source else (other_size, my_size)
        similarity = max(estimate, minhash.containment(estimate, source_size, target_size))
        if similarity >= threshold:
            found.append(
                {
                    "source": source,
                    "target": target,
                    "type": ReuseLink.Type.VERBATIM,
                    "method": ReuseLink.Method.MINHASH,
                    "similarity": round(similarity, 4),
                }
            )
    return found


def find_paraphrase(document: Document, center: np.ndarray | None, layers=None) -> list[dict]:
    """kNN в pgvector по «сырому» косинусу, затем порог по центрированной близости."""
    if document.embedding is None or center is None:
        return []
    threshold = float(get_setting("reuse-paraphrase-cosine"))
    queryset, is_source = counterparts(document, layers)
    with connection.cursor() as cursor:
        # pgvector ≥ 0.8: при фильтре по слою HNSW добирает кандидатов, а не теряет их.
        cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    neighbours = list(
        queryset.filter(embedding__isnull=False)
        .order_by(CosineDistance("embedding", document.embedding))
        .only("id", "embedding", "published_at", "collected_at", "actor_id", "source_id")[
            :KNN_CANDIDATES
        ]
    )
    if not neighbours:
        return []
    mine = centered(document.embedding, center)[0]
    others = centered([n.embedding for n in neighbours], center)
    found = []
    for other, similarity in zip(neighbours, others @ mine, strict=True):
        if similarity >= threshold:
            source, target = _pair(document, other, is_source)
            found.append(
                {
                    "source": source,
                    "target": target,
                    "type": ReuseLink.Type.PARAPHRASE,
                    "method": ReuseLink.Method.KNN,
                    "similarity": round(min(1.0, float(similarity)), 4),
                }
            )
    return found


def clear_automatic(document: Document) -> int:
    """Перед повторной обработкой: автоматические непроверенные связи документа удаляются."""
    deleted, _ = (
        ReuseLink.objects.filter(
            Q(source_document=document) | Q(target_document=document),
            method__in=AUTO_METHODS,
            is_confirmed__isnull=True,
        )
    ).delete()
    return deleted


def save_links(found: list[dict]) -> int:
    """Пишет связи; парафраз не создаётся там, где уже есть дословное совпадение."""
    created = 0
    verbatim = {
        (f["source"].pk, f["target"].pk) for f in found if f["type"] == ReuseLink.Type.VERBATIM
    }
    for item in found:
        pair = (item["source"].pk, item["target"].pk)
        if item["type"] == ReuseLink.Type.PARAPHRASE and (
            pair in verbatim
            or ReuseLink.objects.filter(
                source_document_id=pair[0],
                target_document_id=pair[1],
                link_type=ReuseLink.Type.VERBATIM,
            ).exists()
        ):
            continue
        link, is_new = ReuseLink.objects.get_or_create(
            source_document_id=pair[0],
            target_document_id=pair[1],
            link_type=item["type"],
            defaults={"method": item["method"], "similarity": item["similarity"]},
        )
        if not is_new and link.is_confirmed is None and link.method == item["method"]:
            ReuseLink.objects.filter(pk=link.pk).update(similarity=item["similarity"])
        created += is_new
    return created
