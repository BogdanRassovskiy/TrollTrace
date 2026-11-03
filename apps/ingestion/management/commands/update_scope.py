"""Пересчёт рубрик и признака «в рамках исследования» после смены списков рубрик источника."""

from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.ingestion.extract import page_rubric
from apps.ingestion.scope import rubric_in_scope


class Command(BaseCommand):
    help = (
        "Přepočítá Document.in_scope podle rubrik zdroje. S --reextract nejdřív znovu zjistí "
        "rubriku z uloženého HTML (bez stahování)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, help="Jen dokumenty zdroje s tímto ID.")
        parser.add_argument(
            "--reextract", action="store_true", help="Zjistit rubriku znovu z uloženého HTML."
        )

    def handle(self, *args, source=None, reextract=False, **options):
        documents = Document.objects.select_related("source")
        if source:
            documents = documents.filter(source_id=source)
        changed = reextracted = 0
        for document in documents.iterator(chunk_size=200):
            fields = []
            if reextract and document.source.platform == "web":
                version = document.versions.order_by("-version").first()
                rubric = (
                    page_rubric(version.raw_html, document.title, document.url) if version else ""
                )
                if rubric and rubric != document.rubric:
                    document.rubric = rubric
                    fields.append("rubric")
                    reextracted += 1
            in_scope = rubric_in_scope(document.source, document.rubric)
            if in_scope != document.in_scope:
                document.in_scope = in_scope
                fields.append("in_scope")
            if fields:
                Document.objects.filter(pk=document.pk).update(
                    **{name: getattr(document, name) for name in fields}
                )
                if "in_scope" in fields:
                    Document.objects.filter(pk=document.pk).update(processed_at=None)
                changed += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Změněno dokumentů: {changed} (nová rubrika z HTML: {reextracted}). "
                "Změněné dokumenty se znovu zpracují."
            )
        )
