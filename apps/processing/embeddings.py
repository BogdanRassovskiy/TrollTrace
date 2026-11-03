"""Эмбеддинги (README §4, шаг 4): локальная многоязычная модель, считаются один раз.

Бэкенд выбирается настройкой EMBEDDING_BACKEND:
- "sentence-transformers" — модель из настройки исследования embedding-model (e5);
- "hashing" — детерминированный мешок слов для тестов, без загрузки модели.

Сравнение векторов — после центрирования (см. EmbeddingCenter).
"""

import hashlib
import logging
from functools import lru_cache

import numpy as np
from django.conf import settings

from apps.documents.models import EMBEDDING_DIMENSIONS, Document
from apps.panel.defaults import get_setting

from .models import EmbeddingCenter
from .text import words

logger = logging.getLogger(__name__)

PREFIX = "query: "  # e5: одинаковый префикс для симметричного сравнения текстов
MAX_CHARS = 2000  # ≈512 токенов e5; дальше модель всё равно обрезает
BATCH_SIZE = 16
MIN_CENTER_DOCS = 50  # меньше — средний вектор слишком шумный, семантика отключена
CENTER_SAMPLE = 5000


def model_name() -> str:
    return get_setting("embedding-model")


def document_input(document: Document) -> str:
    title = (document.title or "").strip()
    text = (document.text or "").strip()
    return (f"{title}\n{text}" if title and not text.startswith(title) else text)[:MAX_CHARS]


def input_hash(text: str, model: str) -> str:
    """Хэш входа вместе с моделью: смена модели означает пересчёт эмбеддингов."""
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


@lru_cache(maxsize=2)
def _sentence_transformer(name: str):
    from sentence_transformers import SentenceTransformer  # тяжёлый импорт — только в воркере

    logger.info("Načítám model embeddingů %s", name)
    return SentenceTransformer(name, device="cpu")


def _hashing(texts: list[str]) -> np.ndarray:
    """Признаковое хэширование основ слов (первые 6 букв): похожие тексты — близкие векторы."""
    out = np.zeros((len(texts), EMBEDDING_DIMENSIONS), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in words(text):
            digest = hashlib.blake2b(word[:6].encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "little")
            out[row, value % EMBEDDING_DIMENSIONS] += 1.0 if value >> 63 else -1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1, norms)


def embed(texts: list[str], model: str | None = None) -> np.ndarray:
    """Нормированные векторы (n, 768). Тексты подаются без префикса — он добавляется здесь."""
    if not texts:
        return np.zeros((0, EMBEDDING_DIMENSIONS), dtype=np.float32)
    if settings.EMBEDDING_BACKEND == "hashing":
        return _hashing(texts)
    encoder = _sentence_transformer(model or model_name())
    vectors = encoder.encode(
        [PREFIX + text for text in texts],
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.asarray(vectors, dtype=np.float32)


def embed_documents(documents: list[Document]) -> int:
    """Считает эмбеддинги тем, у кого их нет для текущего текста и модели. Возвращает число."""
    model = model_name()
    todo = []
    for document in documents:
        text = document_input(document)
        digest = input_hash(text, model)
        if document.embedding is None or document.embedding_text_hash != digest:
            todo.append((document, text, digest))
    if not todo:
        return 0
    vectors = embed([text for _, text, _ in todo], model)
    for (document, _, digest), vector in zip(todo, vectors, strict=True):
        document.embedding, document.embedding_text_hash = vector, digest
        Document.objects.filter(pk=document.pk).update(embedding=vector, embedding_text_hash=digest)
    return len(todo)


def compute_center(model: str | None = None) -> EmbeddingCenter | None:
    """Средний вектор по случайной выборке документов с эмбеддингами."""
    model = model or model_name()
    sample = list(
        Document.objects.filter(embedding__isnull=False)
        .order_by("?")
        .values_list("embedding", flat=True)[:CENTER_SAMPLE]
    )
    if len(sample) < MIN_CENTER_DOCS:
        return None
    mean = np.mean(np.asarray(sample, dtype=np.float32), axis=0)
    center, _ = EmbeddingCenter.objects.update_or_create(
        model_name=model, defaults={"vector": mean, "n_documents": len(sample)}
    )
    return center


def get_center(model: str | None = None) -> np.ndarray | None:
    """Центр пространства; вычисляется, когда документов хватает.

    Пока корпус мал, центр пересчитывается при каждом удвоении числа эмбеддингов:
    иначе он навсегда остался бы средним по первой пачке.
    """
    model = model or model_name()
    center = EmbeddingCenter.objects.filter(model_name=model).first()
    if center is None or center.n_documents < CENTER_SAMPLE:
        embedded = Document.objects.filter(embedding__isnull=False).count()
        if center is None or embedded >= 2 * center.n_documents:
            center = compute_center(model) or center
    return None if center is None else np.asarray(center.vector, dtype=np.float32)


def centered(vectors, center: np.ndarray) -> np.ndarray:
    """Центрирование и нормировка: дальше косинус — это скалярное произведение."""
    matrix = np.atleast_2d(np.asarray(vectors, dtype=np.float32)) - center
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)
