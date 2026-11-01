from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    """Включает расширение pgvector. Остальные миграции, хранящие эмбеддинги, зависят от этой."""

    initial = True
    dependencies = []
    operations = [VectorExtension()]
