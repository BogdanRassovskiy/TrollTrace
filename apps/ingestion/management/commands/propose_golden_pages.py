"""Návrh kontrolních stránek (README §4.2) z již sebraných dokumentů s hodnocením 5.

Эталон берётся из уже извлечённого документа (заголовок, дата со страницы, первые
~200 символов). Это предложение: исследователь проверяет его в админке по оригиналу.
"""

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.actors.models import Platform, Source
from apps.documents.models import Document
from apps.ingestion.models import GoldenPage

PREFIX_CHARS = 200
MIN_AGE = datetime.timedelta(days=1)  # после перепроверки через 24 ч правки уже видны


class Command(BaseCommand):
    help = "Navrhne 2–3 kontrolní stránky pro každý webový zdroj z dokumentů s hodnocením 5."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, help="Jen zdroj s tímto ID.")
        parser.add_argument("--per-source", type=int, default=3)

    def handle(self, *args, source=None, per_source=3, **options):
        sources = Source.objects.filter(is_active=True, platform=Platform.WEB)
        if source:
            sources = sources.filter(pk=source)
        created = 0
        for src in sources:
            missing = per_source - src.golden_pages.filter(is_active=True).count()
            if missing <= 0:
                continue
            candidates = (
                Document.objects.filter(
                    source=src,
                    status=Document.Status.ACTIVE,
                    date_source=Document.DateSource.PAGE,
                    collected_at__lte=timezone.now() - MIN_AGE,
                    quality_scores__is_current=True,
                    quality_scores__score=5,
                )
                .exclude(in_scope=False)
                .exclude(url__in=src.golden_pages.values("url"))
                .order_by("collected_at")
            )
            days = set()
            for doc in candidates:
                day = doc.published_at.date()
                if day in days:  # разные дни — разные шаблоны и выпуски страницы
                    continue
                days.add(day)
                GoldenPage.objects.create(
                    source=src,
                    url=doc.url,
                    expected_title=doc.title,
                    expected_published_at=doc.published_at,
                    expected_text_prefix=doc.text[:PREFIX_CHARS],
                )
                created += 1
                missing -= 1
                if missing == 0:
                    break
            self.stdout.write(f"{src.name}: kontrolních stránek {src.golden_pages.count()}")
        self.stdout.write(
            self.style.SUCCESS(
                f"Navrženo {created} kontrolních stránek. Ověřte vzory v administraci "
                "a spusťte manage.py monitor --golden."
            )
        )
