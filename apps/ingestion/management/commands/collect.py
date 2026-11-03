"""Ручной запуск сбора (без Celery) — для отладки и первого наполнения.

python manage.py collect --source 3
python manage.py collect --layer D --max-articles 5
python manage.py collect --all
"""

from django.core.management.base import BaseCommand, CommandError

from apps.actors.models import Source
from apps.ingestion.tasks import run_collector


class Command(BaseCommand):
    help = "Okamžitě sebere dokumenty z uvedených zdrojů a vypíše souhrn."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, action="append", help="ID zdroje.")
        parser.add_argument("--layer", help="Všechny aktivní zdroje vrstvy (A–E).")
        parser.add_argument("--all", action="store_true", help="Všechny aktivní zdroje.")
        parser.add_argument(
            "--max-articles",
            type=int,
            help="Limit nových dokumentů (stránek PSP) na jedno spuštění.",
        )

    def handle(self, *args, source, layer, all, max_articles, **options):
        sources = Source.objects.filter(is_active=True)
        if source:
            sources = Source.objects.filter(pk__in=source)
        elif layer:
            sources = sources.filter(layer=layer.upper())
        elif not all:
            raise CommandError("Zadejte --source, --layer nebo --all.")
        for src in sources:
            if max_articles:
                # Только на этот запуск, в БД не сохраняется.
                src.collector_config = {
                    **src.collector_config,
                    "max_articles": max_articles,
                    "max_pages": max_articles,
                    "backfill_pages": max(1, max_articles // 20),
                }
            run = run_collector(src)
            self.stdout.write(
                f"{src.name}: {run.status} — nalezeno {run.found}, nových {run.new}, "
                f"verzí {run.versions}, duplikátů {run.duplicates}, přeskočeno {run.skipped}, "
                f"chyb {run.errors}, požadavků {run.http_requests}"
            )
            for error in run.details.get("errors", [])[:5]:
                self.stdout.write(f"  ! {error}")
