"""Справочник органов слоя I и их людей (README §2.1), идемпотентно.

    python manage.py import_sources apps/ingestion/sources/security.json
    python manage.py import_institutions

- Источники (ленты сайтов органов) подключаются import_sources; здесь орган связывается
  с ними по адресу, а его актором становится актор этих источников. У органа без ленты
  создаётся собственный актор слоя I — события и документы есть к кому отнести.
- Профессиональные руководители — акторы слоя I (тип official) в любом канале.
- Министры силовых и внешнеполитических ведомств остаются политиками в своём слое:
  существующему актору (депутату PSP) ставится security_portfolio, новый создаётся в слое E.
"""

import datetime
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.actors.models import Actor, Layer, Source
from apps.institutions.models import Institution
from apps.panel import audit

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "institutions.json"
FIELDS = ("abbreviation", "group", "url", "notes")
NOTE = "import_institutions"


def create_actor(**fields) -> Actor:
    criterion = fields.pop("criterion")
    actor = Actor.objects.create(inclusion_criterion=criterion, **fields)
    actor.include(criterion, on=datetime.date.today())
    audit.log_create(None, actor, note=NOTE)
    return actor


class Command(BaseCommand):
    help = "Vytvoří nebo aktualizuje instituce vrstvy I, jejich vedoucí a ministry."

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
                audit.log_create(None, institution, note=NOTE)
                verb = "vytvořena"
            else:
                before = audit.snapshot(institution)
                for key, value in values.items():
                    setattr(institution, key, value)
                institution.save()
                audit.log_change(None, institution, before, note=NOTE)
                verb = "aktualizována"

            missing = self._link_sources(institution, spec.get("sources", []))
            if institution.actor_id is None:
                institution.actor = create_actor(
                    name=spec["name"], actor_type=Actor.Type.INSTITUTION, layer=Layer.SECURITY,
                    criterion="Instituce bezpečnosti — vrstva I (README §2.1)",
                    inclusion_reference=spec.get("url", ""),
                )  # fmt: skip
                institution.save(update_fields=["actor", "updated_at"])
            for person in spec.get("officials", []):
                institution.officials.add(self._official(person))
            if "minister" in spec:
                institution.minister = self._minister(spec["minister"])
                institution.save(update_fields=["minister", "updated_at"])

            line = (
                f"{institution}: {verb}, zdrojů {institution.sources.count()}, "
                f"vedoucích {institution.officials.count()}"
            )
            if institution.minister_id:
                line += f", ministr {institution.minister}"
            if missing:
                line += f" (chybí zdroj — nejdřív import_sources: {', '.join(missing)})"
            self.stdout.write(line)

    def _link_sources(self, institution, urls) -> list[str]:
        missing = []
        for url in urls:
            source = Source.objects.filter(url=url).first()
            if source is None:
                missing.append(url)
                continue
            institution.sources.add(source)
            account = source.accounts.filter(account_id="").first()
            if account is not None and institution.actor_id is None:
                institution.actor = account.actor
                institution.save(update_fields=["actor", "updated_at"])
        return missing

    def _official(self, person: dict) -> Actor:
        actor = Actor.objects.filter(
            name=person["name"], layer=Layer.SECURITY, actor_type=Actor.Type.OFFICIAL
        ).first()
        if actor is None:
            actor = create_actor(
                name=person["name"], actor_type=Actor.Type.OFFICIAL, layer=Layer.SECURITY,
                role=person["role"][:100],
                criterion=f"Profesní vedoucí ({person['role']}) — vrstva I v jakémkoli kanálu "
                f"(README §2.1). {person.get('note', '')}".strip(),
                inclusion_reference=person.get("reference", ""),
            )  # fmt: skip
        return actor

    def _minister(self, person: dict) -> Actor:
        """Министр — политик: существующий актор не переносится в слой I, только флаг."""
        actor = (
            Actor.objects.filter(name=person["name"], actor_type=Actor.Type.POLITICIAN)
            .order_by("layer")
            .first()
        )
        if actor is None:
            return create_actor(
                name=person["name"], actor_type=Actor.Type.POLITICIAN, layer=Layer.PARLIAMENT,
                role=person["role"][:100], security_portfolio=True,
                criterion=f"Člen vlády ({person['role']}): vystupuje ve Sněmovně; resortní "
                "výstupy patří instituci (README §2.1).",
                inclusion_reference=person.get("reference", ""),
            )  # fmt: skip
        if not actor.security_portfolio:
            before = audit.snapshot(actor)
            actor.security_portfolio = True
            actor.save(update_fields=["security_portfolio", "updated_at"])
            audit.log_change(None, actor, before, note=NOTE)
        return actor
