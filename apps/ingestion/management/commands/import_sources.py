"""Подключение источников из JSON-файла (идемпотентно): источник, его актор, ленты, каналы.

    python manage.py import_sources apps/ingestion/sources/mainstream.json

Формат — см. apps/ingestion/sources/README.md.
"""

import datetime
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.actors.models import Actor, ActorAccount, Source
from apps.ingestion.models import SourceFeed
from apps.panel import audit

NOTE = "import_sources"
SOURCE_FIELDS = {
    "layer",
    "platform",
    "collect_interval_minutes",
    "request_delay_seconds",
    "collector_config",
    "attribution_by",
    "attribution_url",
    "external_rating",
    "external_rating_source",
    "expected_language",
    "min_text_length",
    "stale_after_hours",
    "notes",
}


def ensure_actor(source: Source, account_id: str, spec: dict, url: str = "") -> Actor:
    account = ActorAccount.objects.filter(source=source, account_id__iexact=account_id).first()
    if account is not None:
        return account.actor
    actor = Actor.objects.create(
        name=spec["name"],
        actor_type=spec.get("type", Actor.Type.MEDIA),
        layer=spec.get("layer", source.layer),
        party=spec.get("party", ""),
        role=spec.get("role", ""),
        is_hybrid=spec.get("is_hybrid", False),
        inclusion_criterion=spec.get("criterion", ""),
        inclusion_reference=spec.get("reference", ""),
    )
    account = ActorAccount.objects.create(
        actor=actor, source=source, account_id=account_id, url=url
    )
    actor.include(spec.get("criterion", "Подключён к сбору"), on=datetime.date.today())
    audit.log_create(None, actor, note=NOTE)
    audit.log_create(None, account, note=NOTE)
    return actor


class Command(BaseCommand):
    help = "Создаёт или обновляет источники, их акторов и ленты из JSON-файла."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)

    @transaction.atomic
    def handle(self, *args, path, **options):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(f"{path}: {exc}") from exc

        for spec in data["sources"]:
            source = Source.objects.filter(url=spec["url"]).first()
            fields = {k: v for k, v in spec.items() if k in SOURCE_FIELDS}
            if source is None:
                source = Source.objects.create(name=spec["name"], url=spec["url"], **fields)
                audit.log_create(None, source, note=NOTE)
                verb = "создан"
            else:
                before = audit.snapshot(source)
                source.name = spec["name"]
                for key, value in fields.items():
                    setattr(source, key, value)
                source.save()
                audit.log_change(None, source, before, note=NOTE)
                verb = "обновлён"

            for feed in spec.get("feeds", []):
                obj, created = SourceFeed.objects.update_or_create(
                    source=source,
                    url=feed["url"],
                    defaults={"kind": feed.get("kind", "rss"), "rubric": feed.get("rubric", "")},
                )
                if created:
                    audit.log_create(None, obj, note=NOTE)

            if "actor" in spec:  # сайт целиком = один актор (медиа, атрибутированная сеть)
                ensure_actor(source, "", spec["actor"])
            for channel in spec.get("channels", []):
                handle = channel["handle"].lstrip("@")
                ensure_actor(
                    source,
                    handle,
                    {"type": Actor.Type.CHANNEL, **channel.get("actor", {"name": handle})},
                    url=f"https://t.me/{handle}",
                )
            self.stdout.write(f"{source.name}: {verb}, лент {source.feeds.count()}")
