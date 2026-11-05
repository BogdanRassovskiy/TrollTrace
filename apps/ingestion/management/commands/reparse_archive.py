"""Переизвлечение архива веб-документов парсерами сайтов из сохранённого HTML (этап 3.5).

    python manage.py reparse_archive                  # только отчёт: что изменится
    python manage.py reparse_archive --source blesk   # один источник (ключ парсера или id)
    python manage.py reparse_archive --apply          # сохранить: версии, оценки, агрегаты

Без --apply ничего не меняется. С --apply изменившийся текст сохраняется новой версией
и документ заново проходит обработку (эмбеддинги, кандидаты, разметка) — это стоит
запросов к LLM, поэтому сначала стоит посмотреть отчёт.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.actors.models import Platform, Source
from apps.documents.models import Document
from apps.ingestion import quality
from apps.ingestion.tasks import reparse_document

FIELDS = {
    "text": "text",
    "title": "titulek",
    "published_at": "datum",
    "rubric": "rubrika",
    "language": "jazyk",
    "meta": "metadata",
}


class Command(BaseCommand):
    help = "Znovu vytěží webové dokumenty z uloženého HTML parserem zdroje (výchozí: jen přehled)."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", default=[],
                            help="Klíč parseru nebo id zdroje (lze opakovat).")  # fmt: skip
        parser.add_argument("--apply", action="store_true", help="Změny uložit.")
        parser.add_argument("--limit", type=int, default=0, help="Nejvýše dokumentů na zdroj.")

    def handle(self, *args, source, apply, limit, **options):
        sources = Source.objects.filter(platform=Platform.WEB).exclude(parser="")
        if source:
            ids = [int(s) for s in source if s.isdigit()]
            sources = sources.filter(Q(parser__in=source) | Q(pk__in=ids))
            if not sources.exists():
                raise CommandError(f"Zdroj nenalezen: {', '.join(source)}")

        total, rescheduled = Counter(), False
        for src in sources.order_by("layer", "name"):
            docs = Document.objects.filter(source=src).select_related("source").order_by("pk")
            if limit:
                docs = docs[:limit]
            counts = Counter()
            for doc in docs.iterator(chunk_size=200):
                counts["documents"] += 1
                result = reparse_document(doc, apply=apply)
                if result is None:
                    counts["no_html"] += 1
                    continue
                changed, missing = result
                if missing:
                    counts["missing"] += 1
                for name in changed:
                    counts[name] += 1
                if changed:
                    counts["changed"] += 1
                    if apply:
                        quality.assess(doc)
                        rescheduled |= bool({"text", "published_at", "rubric"} & set(changed))
            total.update(counts)
            self.stdout.write(self._line(f"{src.name} ({src.parser})", counts))
        self.stdout.write(self._line("Celkem", total))

        if not apply:
            self.stdout.write("Nic neuloženo — pro uložení spusťte s --apply.")
        elif rescheduled:
            from apps.analytics.tasks import rebuild_aggregates

            rebuild_aggregates.delay()  # даты, рубрики и тексты меняют недельные агрегаты
            self.stdout.write("Agregáty se přepočítají na pozadí.")

    @staticmethod
    def _line(label: str, counts: Counter) -> str:
        fields = ", ".join(f"{name} {counts[key]}" for key, name in FIELDS.items() if counts[key])
        return (
            f"{label}: dokumentů {counts['documents']}, změněno {counts['changed']}"
            + (f" ({fields})" if fields else "")
            + (f", bez HTML {counts['no_html']}" if counts["no_html"] else "")
            + (f", parser nenašel pole {counts['missing']}" if counts["missing"] else "")
        )
