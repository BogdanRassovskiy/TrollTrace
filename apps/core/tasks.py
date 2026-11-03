"""Служебные задачи: пульс Celery для мониторинга работы сервера (этап 4)."""

import logging

import httpx
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "core:heartbeat"
HEARTBEAT_MAX_AGE = 15 * 60  # секунд: три пропущенных пульса — сбор стоит


@shared_task
def ping() -> str:
    """Проверка связки Django ↔ Celery ↔ брокер."""
    return "pong"


@shared_task
def heartbeat() -> str:
    now = timezone.now()
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
