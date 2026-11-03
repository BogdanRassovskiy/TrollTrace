"""Модели обработки (README §4, шаги 3–6)."""

from django.db import models
from pgvector.django import VectorField

from apps.documents.models import EMBEDDING_DIMENSIONS


class EmbeddingCenter(models.Model):
    """Средний вектор корпуса для модели эмбеддингов.

    У e5 все косинусы сжаты в узкую полосу (≈0,80–0,88 у несвязанных текстов),
    поэтому сравнение идёт после вычитания среднего вектора корпуса: несвязанные
    тексты дают ≈0, перевод того же текста ru→cs ≈0,7. Пороги relevance-threshold
    и reuse-paraphrase-cosine заданы именно для центрированной косинусной близости.
    """

    model_name = models.CharField("model", max_length=200, unique=True)
    vector = VectorField("střední vektor", dimensions=EMBEDDING_DIMENSIONS)
    n_documents = models.PositiveIntegerField("počet dokumentů")
    computed_at = models.DateTimeField("vypočteno", auto_now=True)

    class Meta:
        verbose_name = "střed prostoru embeddingů"
        verbose_name_plural = "středy prostoru embeddingů"

    def __str__(self):
        return f"{self.model_name} ({self.n_documents} dok.)"
