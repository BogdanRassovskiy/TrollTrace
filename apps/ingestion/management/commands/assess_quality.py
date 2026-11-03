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
        levels: dict[int, str] = {}
        done = 0
        for doc in docs.iterator(chunk_size=200):
            if doc.source_id not in levels:
                levels[doc.source_id] = quality.open_incident_level(doc.source)
            quality.assess(doc, incident_level=levels[doc.source_id])
            done += 1
        self.stdout.write(f"Ohodnoceno dokumentů: {done} (pravidla {quality.RULES_VERSION}).")
