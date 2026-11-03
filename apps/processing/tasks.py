"""Задачи обработки. Идут в очередь processing (отдельный воркер, модель грузится один раз)."""

from celery import shared_task
from django.core.cache import cache

from .pipeline import process_pending

LOCK_KEY = "processing:lock"
LOCK_TTL = 30 * 60
TIME_BUDGET = 4 * 60  # beat запускает задачу каждые 2 минуты; длинная очередь — по частям


@shared_task
def process_pending_documents() -> dict:
    """Новые и изменённые документы проходят воронку (README §4, шаги 3–6)."""
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiné zpracování"}
    try:
        return process_pending(time_budget=TIME_BUDGET).as_dict()
    finally:
        cache.delete(LOCK_KEY)
