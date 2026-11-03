"""Прогон автоматического обнаружения местных политиков по уже собранным документам."""

from django.core.management.base import BaseCommand

from apps.actors import discovery
from apps.actors.models import ActorMention
from apps.documents.models import Document


class Command(BaseCommand):
    help = (
        "Projde texty dokumentů a přidá starosty, zastupitele apod., jejichž funkce je "
        "v textu výslovně uvedena. Nové dokumenty to dělá zpracování samo; opakování je bezpečné."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, help="Jen dokumenty zdroje s tímto ID.")

    def handle(self, *args, source=None, **options):
        documents = Document.objects.exclude(text="")
        if source:
            documents = documents.filter(source_id=source)
        mentions_before = ActorMention.objects.count()
        created = scanned = 0
        for document in documents.only("id", "url", "title", "text", "published_at",
                                       "collected_at").iterator(chunk_size=500):  # fmt: skip
            created += discovery.record_document(document)
            scanned += 1
        mentions = ActorMention.objects.count() - mentions_before
        self.stdout.write(
            self.style.SUCCESS(
                f"Prošlo dokumentů {scanned}: nových zmínek {mentions}, "
                f"automaticky přidaných aktérů {created}."
            )
        )
