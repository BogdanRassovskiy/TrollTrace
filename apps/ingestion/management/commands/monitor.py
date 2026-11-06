"""Разовый прогон проверок мониторинга (по расписанию — задача monitor_sources)."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.actors.models import Source
from apps.ingestion import monitoring
from apps.ingestion.http import PoliteClient


class Command(BaseCommand):
    help = "Kontroly monitoringu sběru podle zdrojů (README §4.2)."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, help="ID zdroje; výchozí jsou všechny aktivní")
        parser.add_argument("--golden", action="store_true", help="Také kontrolní stránky")

    def handle(self, *args, source, golden, **options):
        sources = Source.objects.filter(is_active=True)
        if source:
            sources = sources.filter(pk=source)
        now = timezone.now()
        for item in sources:
            results = monitoring.monitor_source(item, now)
            if golden:
                with PoliteClient().use_source(item) as client:
                    result = monitoring.check_golden(item, now, client)
                if result is not None:
                    monitoring.apply(item, result, now)
                    results.append(result)
            summary = ", ".join(f"{r.check_type}={r.status}" for r in results)
            self.stdout.write(f"{item.name} [{monitoring.health(item)}]: {summary}")
            for result in results:
                for signal in result.signals:
                    self.stdout.write(f"  ! {signal.incident_type}: {signal.description}")
