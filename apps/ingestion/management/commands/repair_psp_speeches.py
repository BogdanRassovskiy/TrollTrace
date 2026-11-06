"""Ремонт выступлений стенограмм, в которые записан текст всей страницы.

4. 11. 2026 перепроверка после инцидента источника PSP заново извлекла текст общим
экстрактором: в каждое выступление страницы попал текст всей страницы (вторая версия
документа). Причина устранена (этап 3.8). Команда заново разбирает сохранённую страницу
и берёт текст выступления по якорю (#r12, #pokracovani). Сайт не запрашивается; правильный
текст сохраняется новой версией — история остаётся, документ заново проходит обработку.
Без --apply только показывает, что изменится.
"""

from django.core.management.base import BaseCommand

from apps.actors.models import Platform
from apps.documents.models import Document
from apps.ingestion import psp, quality


class Command(BaseCommand):
    help = "Opraví projevy ve stenoprotokolech, do kterých se zapsal text celé stránky."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Změny uložit.")

    def handle(self, *args, apply, **options):
        counts = {"checked": 0, "repaired": 0, "unchanged": 0, "missing": 0}
        pages: dict[int, dict[str, str]] = {}  # id версии со страницей → {якорь: текст}
        docs = Document.objects.filter(source__platform=Platform.PARLIAMENT).order_by("pk")
        for doc in docs.iterator(chunk_size=200):
            counts["checked"] += 1
            version = doc.versions.order_by("-version").first()
            anchor = doc.external_id.rpartition("#")[2]
            if version is None or not version.raw_html_compressed or not anchor:
                counts["missing"] += 1
                continue
            if version.pk not in pages:
                page = psp.parse_steno_page(version.raw_html)
                pages[version.pk] = {s.anchor: s.text for s in page.speeches}
            text = pages[version.pk].get(anchor)
            if not text:
                counts["missing"] += 1
                continue
            if text.strip() == doc.text.strip():
                counts["unchanged"] += 1
                continue
            counts["repaired"] += 1
            if apply:
                doc.record_version(text=text, title=doc.title, raw_html=version.raw_html)
                quality.assess(doc)
        verb = "Opraveno" if apply else "Zkouška (nic nezměněno): k opravě"
        self.stdout.write(
            f"{verb} {counts['repaired']} projevů; v pořádku {counts['unchanged']}, "
            f"bez stránky nebo kotvy {counts['missing']}, zkontrolováno {counts['checked']}."
        )
        if apply and counts["repaired"]:
            self.stdout.write(
                "Opravené dokumenty čekají na zpracování (process_documents): relevance, "
                "převzetí a kandidáti na narativy se přepočítají."
            )
