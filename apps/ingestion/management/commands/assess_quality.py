"""Пересчёт оценок качества документов (после смены правил — RULES_VERSION)."""

from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.ingestion import quality


class Command(BaseCommand):
    help = "Hodnocení kvality 1–5 pro dokumenty bez aktuálního hodnocení podle platných pravidel."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Přepočítat všechny dokumenty")

    def handle(self, *args, all, **options):
        docs = Document.objects.select_related("source")
        if not all:
            docs = docs.exclude(
                quality_scores__is_current=True,
                quality_scores__quality_rules_version=quality.RULES_VERSION,
            )
        done = 0
        for doc in docs.iterator(chunk_size=200):
            # инцидент — на момент извлечения документа, а не сегодняшний (assess сам считает)
            quality.assess(doc)
            done += 1
        self.stdout.write(f"Ohodnoceno dokumentů: {done} (pravidla {quality.RULES_VERSION}).")
