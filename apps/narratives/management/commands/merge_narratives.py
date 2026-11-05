"""Слияние дубля в основной нарратив (метки, дочерние узлы; дубль — в архив).

python manage.py merge_narratives <код дубля> <код основного> [--note "…"]
"""

from django.core.management.base import BaseCommand, CommandError

from apps.narratives.models import Narrative, NarrativeStatus
from apps.narratives.tree import merge


class Command(BaseCommand):
    help = "Sloučí duplicitní narativ do hlavního (štítky, podřízené uzly); duplikát archivuje."

    def add_arguments(self, parser):
        parser.add_argument("duplicate")
        parser.add_argument("keeper")
        parser.add_argument("--note", default="")

    def handle(self, *args, duplicate, keeper, note, **options):
        try:
            dup = Narrative.objects.get(code=duplicate)
            main = Narrative.objects.get(code=keeper)
        except Narrative.DoesNotExist as exc:
            raise CommandError(f"Narativ nenalezen: {exc}") from exc
        if dup.status == NarrativeStatus.ARCHIVED and dup.merged_into_id:
            raise CommandError(f"{dup.code} už je sloučen do {dup.merged_into.code}.")
        moved = merge(dup, main, note=note)
        self.stdout.write(
            f"{dup.code} → {main.code}: převedeno štítků {moved}, duplikát archivován."
        )
        from apps.analytics.tasks import rebuild_aggregates

        rebuild_aggregates.delay()
        self.stdout.write("Agregáty se přepočítají na pozadí.")
