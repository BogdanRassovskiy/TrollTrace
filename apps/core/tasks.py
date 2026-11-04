"""Служебные задачи: пульс Celery для мониторинга работы сервера (этап 4).

Проверка устаревшего кода: Celery не перечитывает код сам. Если миграцию применили, а
воркер не перезапустили, он пишет в БД по старым моделям (так 3–4.11.2026 сбор сутки падал
на NOT NULL «speaking_role»). Миграции, известные коду, запоминаются при старте процесса;
применённая в БД миграция, которой среди них нет, значит, что код воркера устарел.
"""

import datetime
import logging

import httpx
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "core:heartbeat"
HEARTBEAT_MAX_AGE = 15 * 60  # секунд: три пропущенных пульса — сбор стоит


def _disk_migrations() -> set[tuple[str, str]]:
    from django.db.migrations.loader import MigrationLoader

    return set(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)


_KNOWN_MIGRATIONS: set[tuple[str, str]] | None = None


def remember_code_migrations() -> None:
    """Вызывается при старте воркера (config/celery.py): снимок миграций кода процесса."""
    global _KNOWN_MIGRATIONS
    _KNOWN_MIGRATIONS = _disk_migrations()


def unknown_applied_migrations() -> list[str]:
    """Применённые в БД миграции, которых не знает код этого процесса."""
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    known = _KNOWN_MIGRATIONS if _KNOWN_MIGRATIONS is not None else _disk_migrations()
    applied = set(MigrationRecorder(connection).applied_migrations())
    apps_in_code = {app for app, _ in known}
    return sorted(f"{a}.{n}" for a, n in applied - known if a in apps_in_code)


def check_stale_code(role: str) -> list[str]:
    """Критическое уведомление (раз в сутки на роль), если код процесса устарел."""
    unknown = unknown_applied_migrations()
    if not unknown:
        return []
    from apps.panel.models import Notification

    title = f"Celery ({role}) běží se zastaralým kódem: restartujte ho"
    logger.error("%s; neznámé migrace: %s", title, ", ".join(unknown))
    since = timezone.now() - datetime.timedelta(days=1)
    if not Notification.objects.filter(title=title, created_at__gte=since).exists():
        Notification.objects.create(
            notification_type=Notification.Type.OTHER,
            level=Notification.Level.CRITICAL,
            title=title,
            text="V databázi jsou použité migrace, které proces nezná ("
            + ", ".join(unknown)
            + "). Zápisy podle starých modelů selhávají. Spusťte: docker compose restart "
            "worker processor beat.",
        )
    return unknown


@shared_task
def ping() -> str:
    """Проверка связки Django ↔ Celery ↔ брокер."""
    return "pong"


@shared_task
def heartbeat() -> str:
    now = timezone.now()
    if check_stale_code("worker"):
        return "zastaralý kód"  # bez pulsu: externí hlídač také upozorní
    cache.set(HEARTBEAT_KEY, now.isoformat(), timeout=24 * 60 * 60)
    if settings.HEARTBEAT_PING_URL:
        try:
            httpx.get(settings.HEARTBEAT_PING_URL, timeout=10)
        except httpx.HTTPError as exc:
            logger.warning("Heartbeat: ping se nezdařil: %s", exc)
    return now.isoformat()


def heartbeat_age(now=None) -> float | None:
    """Сколько секунд прошло с последнего пульса; None — пульса ещё не было."""
    value = cache.get(HEARTBEAT_KEY)
    if not value:
        return None
    from datetime import datetime

    return ((now or timezone.now()) - datetime.fromisoformat(value)).total_seconds()
