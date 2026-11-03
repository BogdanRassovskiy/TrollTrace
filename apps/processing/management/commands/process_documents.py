"""Ручной запуск воронки обработки: дообработка очереди или пересчёт после смены порогов."""

from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.processing.embeddings import compute_center
from apps.processing.pipeline import process_pending, reset


class Command(BaseCommand):
    help = "Zpracuje dokumenty ve frontě: relevance, embeddingy, doslovná převzetí a parafráze."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Vrátit do fronty všechny dokumenty (po změně prahů nebo kódové knihy). "
            "Embeddingy se znovu nepočítají, pokud se text nezměnil.",
        )
        parser.add_argument("--source", type=int, help="Jen dokumenty zdroje s tímto ID.")
        parser.add_argument("--limit", type=int, help="Zpracovat nejvýše N dokumentů.")
        parser.add_argument(
            "--recenter",
            action="store_true",
            help="Přepočítat střed prostoru embeddingů z aktuálního korpusu.",
        )

    def handle(self, *args, **options):
        if options["all"] or options["source"]:
            queryset = Document.objects.all()
            if options["source"]:
                queryset = queryset.filter(source_id=options["source"])
            self.stdout.write(f"Do fronty vráceno dokumentů: {reset(queryset)}")
        if options["recenter"]:
            center = compute_center()
            self.stdout.write(
                f"Střed přepočítán: {center}" if center else "Na výpočet středu je málo embeddingů."
            )
        stats = process_pending(limit=options["limit"])
        if options["recenter"] and stats.embedded:
            compute_center()  # первый прогон: центр по полному корпусу, а не по первой пачке
        self.stdout.write(
            self.style.SUCCESS(
                f"Zpracováno {stats.processed}: relevantních {stats.relevant}, "
                f"nerelevantních {stats.irrelevant}, nových embeddingů {stats.embedded}, "
                f"nových převzetí {stats.links}, automaticky přidaných aktérů {stats.actors}."
            )
        )
