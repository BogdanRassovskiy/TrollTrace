"""Все депутаты текущего созыва PSP → акторы слоя E, с иконками фото.

python manage.py import_psp_deputies                 # последний созыв, фото только новым
python manage.py import_psp_deputies --term PSP9 --refresh-photos
"""

from django.core.management.base import BaseCommand

from apps.ingestion.deputies import sync_deputies
from apps.ingestion.http import PoliteClient


class Command(BaseCommand):
    help = "Importuje poslance Poslanecké sněmovny z otevřených dat psp.cz."

    def add_arguments(self, parser):
        parser.add_argument("--term", help="Kód volebního období (PSP10). Výchozí je poslední.")
        parser.add_argument("--no-photos", action="store_true", help="Nestahovat fotografie.")
        parser.add_argument(
            "--refresh-photos", action="store_true", help="Znovu stáhnout všechny fotografie."
        )

    def handle(self, *args, term, no_photos, refresh_photos, **options):
        with PoliteClient(delay=1.0) as client:
            result = sync_deputies(
                client, term_code=term, photos=not no_photos, refresh_photos=refresh_photos
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Vytvořeno: {result.created}, aktualizováno: {result.updated}, "
                f"beze změn: {result.unchanged}, fotografií: {result.photos}."
            )
        )
        for error in result.photo_errors:
            self.stderr.write(f"Foto: {error}")
