"""Ручной запуск сбора (без Celery) — для отладки и первого наполнения.

python manage.py collect --source 3
python manage.py collect --layer D --max-articles 5
python manage.py collect --all
"""

from django.core.management.base import BaseCommand, CommandError

from apps.actors.models import Source
from apps.ingestion.tasks import run_collector


class Command(BaseCommand):
    help = "Собирает документы из указанных источников сейчас и печатает итог."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, action="append", help="ID источника.")
        parser.add_argument("--layer", help="Все активные источники слоя (A–E).")
        parser.add_argument("--all", action="store_true", help="Все активные источники.")
        parser.add_argument(
            "--max-articles", type=int, help="Предел новых документов (страниц PSP) за запуск."
        )

    def handle(self, *args, source, layer, all, max_articles, **options):
        sources = Source.objects.filter(is_active=True)
        if source:
            sources = Source.objects.filter(pk__in=source)
        elif layer:
            sources = sources.filter(layer=layer.upper())
        elif not all:
            raise CommandError("Укажите --source, --layer или --all.")
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
                f"{src.name}: {run.status} — найдено {run.found}, новых {run.new}, "
                f"версий {run.versions}, дублей {run.duplicates}, пропущено {run.skipped}, "
                f"ошибок {run.errors}, запросов {run.http_requests}"
            )
            for error in run.details.get("errors", [])[:5]:
                self.stdout.write(f"  ! {error}")
