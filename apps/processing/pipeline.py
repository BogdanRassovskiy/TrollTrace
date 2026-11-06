"""Воронка обработки (README §4, шаги 3–6). Каждый новый документ проходит её один раз.

1. Дешёвые фильтры: пустой текст, качество 1, рубрика вне исследования → нерелевантен.
2. Эмбеддинг — один раз на текст и модель.
3. Релевантность: ключевые слова тем или близость к темам и нарративам кодбука.
4. MinHash-подпись и LSH-пояса.
5. Заимствования из слоя-источника: дословные (MinHash/LSH) и парафразы (kNN).
6. Кандидаты в разметку нарративов (README §4, шаг 7): близость к нарративам кодбука и
   сигнальные слова → ожидающие решения LLM (apps/processing/labeling.py, отдельная задача).
7. Местные политики, функция которых прямо названа в тексте (starosta, zastupitel …), —
   в акторы с пометкой «přidán automaticky» (apps/actors/discovery.py). Для всех документов,
   в том числе нерелевантных: региональные рубрики тоже источник таких упоминаний.

Новая редакция текста (DocumentVersion) сбрасывает processed_at — документ пройдёт снова.
"""

import logging
import time
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.actors import discovery
from apps.documents.models import Document
from apps.panel.defaults import get_setting

from . import labeling, minhash, relevance, reuse
from .embeddings import embed_documents, get_center, model_name

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


@dataclass
class Stats:
    processed: int = 0
    embedded: int = 0
    relevant: int = 0
    irrelevant: int = 0
    links: int = 0
    actors: int = 0
    candidates: int = 0
    screenings: int = 0

    def add(self, other: "Stats"):
        for key in self.__dataclass_fields__:
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def pending() -> "Document.objects":
    return Document.objects.filter(processed_at__isnull=True).order_by("id")


def process_documents(documents: list[Document]) -> Stats:
    stats = Stats()
    if not documents:
        return stats
    model = model_name()
    shingle_size = int(get_setting("minhash-shingle-size"))
    reasons = {doc.pk: relevance.gate(doc) for doc in documents}
    passed = [doc for doc in documents if reasons[doc.pk] is None]
    stats.embedded = embed_documents(passed)
    center = get_center(model)
    codebook = relevance.load_codebook(model)
    prototypes = labeling.load_prototypes()
    for document in documents:
        with transaction.atomic():
            stats.links += _process_one(
                document, reasons[document.pk], codebook, center, shingle_size
            )
            stats.actors += discovery.record_document(document)
            labeling.drop_stale_labels(document)
            if document.is_relevant:
                stats.candidates += labeling.queue_candidates(document, prototypes, center)
                stats.screenings += labeling.queue_screening(document)
        stats.processed += 1
        if document.is_relevant:
            stats.relevant += 1
        elif document.is_relevant is False:
            stats.irrelevant += 1
    return stats


def _process_one(document, reason, codebook, center, shingle_size) -> int:
    reuse.clear_automatic(document)
    if reason:
        document.is_relevant, document.relevance_score = False, None
        document.relevance_details = {"reason": reason}
        document.minhash = document.minhash_bands = None
    else:
        decision = relevance.decide(document, codebook, center)
        document.is_relevant = decision["is_relevant"]
        document.relevance_score = decision["score"]
        document.relevance_details = decision["details"]
        hashvalues = minhash.signature(minhash.shingles(document.text, shingle_size))
        if hashvalues is None:
            document.minhash = document.minhash_bands = None
        else:
            document.minhash = minhash.to_bytes(hashvalues)
            document.minhash_bands = minhash.bands(hashvalues)
    document.processed_at = timezone.now()
    Document.objects.filter(pk=document.pk).update(
        is_relevant=document.is_relevant,
        relevance_score=document.relevance_score,
        relevance_details=document.relevance_details,
        minhash=document.minhash,
        minhash_bands=document.minhash_bands,
        processed_at=document.processed_at,
    )
    if reason:
        return 0
    found = reuse.find_verbatim(document, shingle_size=shingle_size)
    found += reuse.find_paraphrase(document, center)
    return reuse.save_links(found)


def process_pending(limit: int | None = None, time_budget: float | None = None) -> Stats:
    """Обрабатывает очередь пачками, пока она не пуста, не достигнут limit или бюджет времени."""
    total = Stats()
    started = time.monotonic()
    while limit is None or total.processed < limit:
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - total.processed)
        batch = list(pending().select_related("source")[:size])
        if not batch:
            break
        total.add(process_documents(batch))
        if time_budget is not None and time.monotonic() - started > time_budget:
            break
    if total.processed:
        logger.info("Zpracování: %s", total.as_dict())
    return total


def reset(queryset=None) -> int:
    """Возвращает документы в очередь (пересчёт после смены порогов или кодбука).

    Эмбеддинги не пересчитываются, если текст и модель не менялись.
    """
    queryset = Document.objects.all() if queryset is None else queryset
    return queryset.update(processed_at=None)
