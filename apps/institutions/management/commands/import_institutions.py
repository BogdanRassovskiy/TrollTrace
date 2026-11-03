"""Справочник органов слоя I (README §2.1), идемпотентно.

    python manage.py import_sources apps/ingestion/sources/security.json
    python manage.py import_institutions

Источники (ленты сайтов органов) подключаются import_sources; здесь орган связывается
с ними по адресу, а его актором становится актор этих источников.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.actors.models import Source
from apps.institutions.models import Institution
from apps.panel import audit

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "institutions.json"
FIELDS = ("abbreviation", "group", "url", "notes")


class Command(BaseCommand):
    help = "Vytvoří nebo aktualizuje instituce vrstvy I a propojí je s jejich zdroji."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, nargs="?", default=DEFAULT_PATH)

    @transaction.atomic
    def handle(self, *args, path, **options):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(f"{path}: {exc}") from exc

        for spec in data["institutions"]:
            institution = Institution.objects.filter(name=spec["name"]).first()
            values = {key: spec.get(key, "") for key in FIELDS}
            if institution is None:
                institution = Institution.objects.create(name=spec["name"], **values)
                audit.log_create(None, institution, note="import_institutions")
                verb = "vytvořena"
            else:
                before = audit.snapshot(institution)
                for key, value in values.items():
                    setattr(institution, key, value)
                institution.save()
                audit.log_change(None, institution, before, note="import_institutions")
                verb = "aktualizována"

            missing = []
            for url in spec.get("sources", []):
                source = Source.objects.filter(url=url).first()
                if source is None:
                    missing.append(url)
                    continue
                institution.sources.add(source)
                account = source.accounts.filter(account_id="").first()
                if account is not None and institution.actor_id is None:
                    institution.actor = account.actor
                    institution.save(update_fields=["actor", "updated_at"])
            line = f"{institution}: {verb}, zdrojů {institution.sources.count()}"
            if missing:
                line += f" (chybí zdroj — nejdřív import_sources: {', '.join(missing)})"
            self.stdout.write(line)
