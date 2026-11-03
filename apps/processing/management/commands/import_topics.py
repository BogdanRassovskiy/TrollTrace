"""Импорт стартовой таксономии тем (data/topics.json). Повторный запуск безопасен."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.narratives.models import Topic
from apps.panel import audit

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "data" / "topics.json"


class Command(BaseCommand):
    help = "Naimportuje témata s klíčovými slovy. Existující témata se aktualizují jen s --update."

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
        parser.add_argument(
            "--update", action="store_true", help="Přepsat popis a klíčová slova existujících."
        )

    def handle(self, *args, file, update, **options):
        created = updated = 0
        for item in json.loads(Path(file).read_text(encoding="utf-8")):
            topic = Topic.objects.filter(code=item["code"]).first()
            if topic is None:
                topic = Topic.objects.create(**item)
                audit.log_create(None, topic, note="Import témat", username="import_topics")
                created += 1
            elif update:
                before = audit.snapshot(topic)
                for key in ("name", "description", "keywords"):
                    setattr(topic, key, item[key])
                topic.save()
                if audit.log_change(
                    None, topic, before, note="Import témat", username="import_topics"
                ):
                    updated += 1
        self.stdout.write(self.style.SUCCESS(f"Témata: nová {created}, aktualizovaná {updated}."))
        if created or updated:
            self.stdout.write("Pro přepočet relevance spusťte: manage.py process_documents --all")
