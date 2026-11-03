"""Импорт стартового кодбука (data/codebook.json). Повторный запуск безопасен.

Изменённое определение создаёт новую версию нарратива, а сигнал версии запускает
ретроспективную разметку архива (README §3).
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.narratives.models import Narrative, Topic
from apps.panel import audit

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "data" / "codebook.json"
FIELDS = (
    "kind", "name", "definition", "examples", "counter_examples", "stance_guide", "cues",
    "attribution",
)  # fmt: skip


class Command(BaseCommand):
    help = "Naimportuje narativy kódové knihy. Existující se aktualizují jen s --update."

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
        parser.add_argument(
            "--update", action="store_true", help="Přepsat definice existujících narativů."
        )

    def handle(self, *args, file, update, **options):
        created = updated = 0
        for item in json.loads(Path(file).read_text(encoding="utf-8")):
            topic = Topic.objects.filter(code=item["topic"]).first()
            if topic is None:
                raise CommandError(
                    f"Téma „{item['topic']}“ neexistuje: nejdřív spusťte import_topics."
                )
            values = {key: item.get(key, Narrative._meta.get_field(key).get_default())
                      for key in FIELDS}  # fmt: skip
            narrative = Narrative.objects.filter(code=item["code"]).first()
            if narrative is None:
                narrative = Narrative(code=item["code"], topic=topic, **values)
                narrative.save(change_note="Import kódové knihy")
                audit.log_create(
                    None, narrative, note="Import kódové knihy", username="import_codebook"
                )
                created += 1
            elif update:
                before = audit.snapshot(narrative)
                narrative.topic = topic
                for key, value in values.items():
                    setattr(narrative, key, value)
                version = narrative.current_version
                narrative.save(change_note="Import kódové knihy (aktualizace)")
                if narrative.current_version != version:
                    audit.log_change(
                        None, narrative, before, note="Import kódové knihy",
                        username="import_codebook",
                    )  # fmt: skip
                    updated += 1
        self.stdout.write(self.style.SUCCESS(f"Narativy: nové {created}, aktualizované {updated}."))
