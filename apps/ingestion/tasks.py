"""Задачи Celery: сбор по расписанию, перепроверка свежих статей, синхронизация депутатов."""

import datetime
import logging

from celery import shared_task
from django.core.cache import cache
from django.db.models import F, Max, Q
from django.utils import timezone

from apps.actors.models import Platform, Source
from apps.documents.models import Document

from .collectors import collector_for
from .extract import extract_article
from .http import PoliteClient, RobotsDisallowed
from .models import CollectionRun

logger = logging.getLogger(__name__)

LOCK_TTL = 2 * 60 * 60  # дольше самого долгого запуска (CELERY_TASK_TIME_LIMIT = 1 ч)
RECHECK_AFTER = [datetime.timedelta(days=1), datetime.timedelta(days=7)]  # README §4.1


def run_collector(source: Source, client: PoliteClient | None = None) -> CollectionRun:
    """Один запуск сбора по источнику с записью CollectionRun. Используется и командой collect."""
    run = CollectionRun.objects.create(source=source)
    client = client or PoliteClient(delay=source.request_delay_seconds)
    collector = collector_for(source)(source, client)
    status = CollectionRun.Status.OK
    try:
        collector.collect()
    except Exception as exc:  # noqa: BLE001 — запуск фиксируется как упавший, а не теряется
        logger.exception("Сбор %s упал", source)
        collector.error(f"{type(exc).__name__}: {exc}")
        status = CollectionRun.Status.FAILED
    finally:
        client.close()
    stats = collector.stats
    if status == CollectionRun.Status.OK and stats["errors"]:
        saved = stats["new"] + stats["versions"] + stats["unchanged"]
        status = (
            CollectionRun.Status.PARTIAL if saved or stats["found"] else CollectionRun.Status.FAILED
        )
    for name in ("found", "new", "versions", "unchanged", "duplicates", "skipped", "errors"):
        setattr(run, name, stats[name])
    run.http_requests = client.stats["requests"]
    run.details = {
        "errors": collector.errors,
        "http": {k: v for k, v in client.stats.items() if k.startswith("http_")},
    }
    run.status = status
    run.finished_at = timezone.now()
    run.save()
    return run


@shared_task
def collect_source(source_id: int) -> dict:
    lock = f"ingestion:collect:{source_id}"
    if not cache.add(lock, "1", LOCK_TTL):
        return {"source": source_id, "skipped": "уже выполняется"}
    try:
        run = run_collector(Source.objects.get(pk=source_id))
        return {"source": source_id, "run": run.pk, "status": run.status, "new": run.new}
    finally:
        cache.delete(lock)


def due_sources(now=None):
    """Активные источники, у которых с последнего запуска прошло не меньше их интервала."""
    now = now or timezone.now()
    sources = Source.objects.filter(is_active=True).annotate(
        last_run=Max("collection_runs__started_at")
    )
    return [
        s
        for s in sources
        if s.last_run is None
        or s.last_run <= now - datetime.timedelta(minutes=s.collect_interval_minutes)
    ]


@shared_task
def collect_due_sources() -> list[int]:
    """Запускается beat'ом каждые 5 минут; сами источники собираются со своим интервалом."""
    ids = [s.pk for s in due_sources()]
    for source_id in ids:
        collect_source.delay(source_id)
    return ids


def documents_to_recheck(now=None):
    """Статьи с сайтов через 24 ч и через 7 дней после сбора (фиксация правок и удалений)."""
    now = now or timezone.now()
    first, second = RECHECK_AFTER
    return (
        Document.objects.filter(
            source__platform=Platform.WEB,
            status=Document.Status.ACTIVE,
            collected_at__gte=now - second * 2,
        )
        .filter(
            Q(last_checked_at__isnull=True, collected_at__lte=now - first)
            | Q(collected_at__lte=now - second, last_checked_at__lt=F("collected_at") + second)
        )
        .select_related("source")
        .order_by("collected_at")
    )


def recheck_document(doc: Document, client: PoliteClient) -> str:
    now = timezone.now()
    try:
        result = client.get(doc.url)
    except RobotsDisallowed:
        return "robots"
    if result.status in (404, 410):
        doc.status, doc.deleted_at, doc.last_checked_at = Document.Status.DELETED, now, now
        doc.save(update_fields=["status", "deleted_at", "last_checked_at"])
        return "deleted"
    if not result.ok:
        return f"http_{result.status}"
    extracted = extract_article(result.text, result.final_url)
    version = doc.record_version(text=extracted.text, title=extracted.title, raw_html=result.text)
    doc.last_checked_at = now
    doc.save(update_fields=["last_checked_at"])
    return "version" if version else "unchanged"


@shared_task
def recheck_documents(limit: int = 200) -> dict:
    if not cache.add("ingestion:recheck", "1", LOCK_TTL):
        return {"skipped": "уже выполняется"}
    counts: dict[str, int] = {}
    client = PoliteClient()
    try:
        for doc in documents_to_recheck()[:limit]:
            client.delay = doc.source.request_delay_seconds
            try:
                outcome = recheck_document(doc, client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Перепроверка %s: %s", doc.url, exc)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
    finally:
        client.close()
        cache.delete("ingestion:recheck")
    return counts


@shared_task
def sync_psp_deputies() -> dict:
    """Раз в неделю: новые депутаты (замены), смена клуба, конец мандата."""
    from .deputies import sync_deputies

    with PoliteClient(delay=1.0) as client:
        result = sync_deputies(client)
    return {"created": result.created, "updated": result.updated, "photos": result.photos}
