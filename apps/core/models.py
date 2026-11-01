"""Базовые абстрактные модели, общие для всех приложений."""

from django.db import models


class TimeStampedModel(models.Model):
    """Время создания и последнего изменения записи."""

    created_at = models.DateTimeField("создано", auto_now_add=True)
    updated_at = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        abstract = True
