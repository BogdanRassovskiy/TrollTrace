"""Все депутаты текущего созыва PSP → акторы слоя E, с иконками фото.

python manage.py import_psp_deputies                 # последний созыв, фото только новым
python manage.py import_psp_deputies --term PSP9 --refresh-photos
"""

from django.core.management.base import BaseCommand

from apps.ingestion.deputies import sync_deputies
from apps.ingestion.http import PoliteClient


class Command(BaseCommand):
    help = "Импортирует депутатов Poslanecké sněmovny из открытых данных psp.cz."

    def add_arguments(self, parser):
        parser.add_argument("--term", help="Код созыва (PSP10). По умолчанию — последний.")
        parser.add_argument("--no-photos", action="store_true", help="Не скачивать фото.")
        parser.add_argument("--refresh-photos", action="store_true", help="Перекачать все фото.")

    def handle(self, *args, term, no_photos, refresh_photos, **options):
        with PoliteClient(delay=1.0) as client:
            result = sync_deputies(
                client, term_code=term, photos=not no_photos, refresh_photos=refresh_photos
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Создано: {result.created}, обновлено: {result.updated}, "
                f"без изменений: {result.unchanged}, фото: {result.photos}."
            )
        )
        for error in result.photo_errors:
            self.stderr.write(f"Фото: {error}")
