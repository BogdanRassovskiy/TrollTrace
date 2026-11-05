"""Задачи Celery: сбор по расписанию, перепроверка свежих статей, синхронизация депутатов."""

import datetime
import logging

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from apps.actors.models import Platform, Source
from apps.documents.models import Document

from . import parsers
from .collectors import collector_for
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
        logger.exception("Sběr %s selhal", source)
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
        "timeouts": client.stats["timeouts"],
        "redirect_offsite": client.stats["redirect_offsite"],
        "robots_disallowed": client.stats["robots_disallowed"],
        # Статьи, где парсер сайта не нашёл обязательное поле (этап 3.5).
        "selectors_missing": stats["selectors_missing"],
    }
    run.status = status
    run.finished_at = timezone.now()
    run.save()
    if source.platform == Platform.WEB:
        # Strukturní otisk hned po sběru: změna rozvržení se ukáže do hodiny, ne až ráno.
        from .monitoring import apply, check_selectors

        try:
            if (result := check_selectors(source, run.finished_at)) is not None:
                apply(source, result, run.finished_at)
        except Exception:  # noqa: BLE001 — сбой проверки не должен ронять запуск сбора
            logger.exception("Kontrola selektorů %s selhala", source)
    return run


@shared_task
def collect_source(source_id: int) -> dict:
    lock = f"ingestion:collect:{source_id}"
    if not cache.add(lock, "1", LOCK_TTL):
        return {"source": source_id, "skipped": "už probíhá"}
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
    extracted = parsers.extract(doc.source, result.text, result.final_url, doc.published_at)
    version = doc.record_version(text=extracted.text, title=extracted.title, raw_html=result.text)
    doc.last_checked_at = now
    fields = ["last_checked_at"]
    if extracted.parser:
        doc.meta = parsers.merge_meta(doc.meta, extracted)
        fields.append("meta")
    doc.save(update_fields=fields)
    return "version" if version else "unchanged"


@shared_task
def recheck_documents(limit: int = 200) -> dict:
    if not cache.add("ingestion:recheck", "1", LOCK_TTL):
        return {"skipped": "už probíhá"}
    counts: dict[str, int] = {}
    client = PoliteClient()
    try:
        for doc in documents_to_recheck()[:limit]:
            client.delay = doc.source.request_delay_seconds
            try:
                outcome = recheck_document(doc, client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Kontrola %s: %s", doc.url, exc)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
    finally:
        client.close()
        cache.delete("ingestion:recheck")
    return counts


@shared_task
def sync_psp_deputies() -> dict:
    """Раз в неделю: депутаты (замены, клуб, конец мандата), евродепутаты, правительство."""
    from apps.actors.management.commands.sync_politicians import run as sync_politicians

    from .deputies import sync_deputies

    with PoliteClient(delay=1.0) as client:
        result = sync_deputies(client)
    summary = {"created": result.created, "updated": result.updated, "photos": result.photos}
    try:  # европарламент и правительство — в той же еженедельной задаче
        for key, other in sync_politicians().items():
            summary[key] = {"created": other.created, "closed": other.closed}
    except Exception:  # noqa: BLE001 — сбой чужого реестра не отменяет синхронизацию депутатов
        logger.exception("Synchronizace europoslanců a vlády selhala")
    return summary


STALE_RUN_ERROR = "Běh nebyl dokončen: pracovník byl přerušen (restart nebo časový limit)."


def close_stale_runs(now=None) -> int:
    """Запуски, оставшиеся «в процессе» после перезапуска воркера, закрываются как упавшие.

    Блокировка источника живёт LOCK_TTL — запуск старше неё точно уже не идёт.
    """
    now = now or timezone.now()
    stale = CollectionRun.objects.filter(
        status=CollectionRun.Status.RUNNING,
        started_at__lt=now - datetime.timedelta(seconds=LOCK_TTL),
    )
    return stale.update(
        status=CollectionRun.Status.FAILED, finished_at=now, details={"errors": [STALE_RUN_ERROR]}
    )


# Документ с такими признаками при перепроверке скачивается заново: возможно, при инциденте
# сохранилась страница согласия, антибота или заглушка. Короткий текст — не повод: он часто
# настоящий (репост из Telegram — заголовок и ссылка).
REFETCH_FLAGS = ("text_empty", "blocked_page", "boilerplate_dup")
MAX_REFETCH = 300


def incident_documents(incident):
    """Документы источника, собранные или изменённые во время инцидента.

    Начало окна — за сутки до открытия: мониторинг судит по документам за прошедший день.
    """
    start = incident.started_at - datetime.timedelta(days=1)
    end = incident.ended_at or timezone.now()
    return (
        Document.objects.filter(source=incident.source)
        .filter(
            Q(collected_at__gte=start, collected_at__lte=end)
            | Q(versions__detected_at__gte=start, versions__detected_at__lte=end)
        )
        .distinct()
    )


def reparse_document(doc: Document, *, apply: bool = True) -> tuple[list[str], list[str]] | None:
    """Повторный разбор сохранённого HTML парсером источника (без запроса к сайту).

    Возвращает (изменившиеся поля — text, title, published_at, rubric, language, meta;
    поля, которые парсер не нашёл) или None, если HTML не сохранён. С apply=False только
    сравнивает (reparse_archive без --apply).
    Новый текст сохраняется версией и заново проходит обработку. Текст и заголовок — из
    последней версии страницы, дата — из первой: ČTK под тем же адресом публикует
    обновлённую заметку с новым «Vydáno», а документ датируется первой публикацией.
    Дата меняется, только если парсер нашёл её на странице (дату из ленты он не знает).
    """
    version = doc.versions.order_by("-version").first()
    if version is None or not version.raw_html_compressed:
        return None
    html = version.raw_html
    url = (doc.meta or {}).get("final_url") or doc.url
    extracted = parsers.extract(doc.source, html, url, doc.published_at)
    first = doc.versions.order_by("version").first()
    dated = extracted
    if first.pk != version.pk and first.raw_html_compressed:
        dated = parsers.extract(doc.source, first.raw_html, url, doc.published_at)

    changes = {}
    if extracted.text and extracted.text.strip() != doc.text.strip():
        changes["text"] = extracted.text
    if extracted.title and extracted.title != doc.title:
        changes["title"] = extracted.title
    if (
        dated.date_source == Document.DateSource.PAGE
        and dated.published_at is not None
        and (
            doc.date_source != dated.date_source
            or doc.published_at is None
            # секунды и миллисекунды страница то показывает, то нет — это не новая дата
            or abs(doc.published_at - dated.published_at) >= datetime.timedelta(minutes=1)
        )
    ):
        changes["published_at"] = dated.published_at
    # Рубрика — только если её не было: у части документов она из ленты, и на её формат
    # опираются rubrics_in_scope (как и в save_item).
    if extracted.rubric and not doc.rubric:
        changes["rubric"] = extracted.rubric[:200]
    if extracted.language and extracted.language != doc.language:
        changes["language"] = extracted.language
    meta = parsers.merge_meta(doc.meta, extracted) if extracted.parser else doc.meta
    if meta != doc.meta:
        changes["meta"] = meta
    if not apply or not changes:
        return list(changes), extracted.missing

    from .scope import rubric_in_scope

    with transaction.atomic():
        title = changes.get("title", doc.title)
        if "text" in changes:
            doc.record_version(text=changes["text"], title=title, raw_html=html)
        doc.title = title
        if "published_at" in changes:
            doc.published_at, doc.date_source = changes["published_at"], Document.DateSource.PAGE
        if "rubric" in changes:
            doc.rubric = changes["rubric"]
            doc.in_scope = rubric_in_scope(doc.source, doc.rubric)
        doc.language = changes.get("language", doc.language)
        doc.meta = meta
        doc.save()
    return list(changes), extracted.missing


def reextract_document(doc: Document) -> bool:
    """Повторное извлечение после инцидента. True — текст изменился (новая версия)."""
    result = reparse_document(doc)
    return result is not None and "text" in result[0]


def recover_duplicates(doc: Document, client: PoliteClient) -> tuple[int, int]:
    """URL, отброшенные как «тот же текст» из-за ошибки извлечения, собираются заново.

    Если текст и правда тот же, сборщик снова запишет URL в duplicate_urls.
    Возвращает (новых документов, ошибок при скачивании).
    """
    from .collectors.web import FeedEntry, WebCollector

    urls = list((doc.meta or {}).get("duplicate_urls") or [])
    if not urls or doc.source.platform != Platform.WEB:
        return 0, 0
    doc.meta = {k: v for k, v in doc.meta.items() if k != "duplicate_urls"}
    doc.save(update_fields=["meta"])
    collector = WebCollector(doc.source, client)
    known = set(
        Document.objects.filter(source=doc.source, url__in=urls).values_list("url", flat=True)
    )
    for url in urls:
        if url not in known:
            collector._collect_article(doc.actor, FeedEntry(url))
    return collector.stats["new"], collector.stats["errors"]


def recheck_incident_documents(incident, client: PoliteClient | None = None) -> dict:
    """После закрытия инцидента: подозрительные документы скачиваются заново (с учётом
    robots.txt и пауз), затем у всех документов окна пересчитывается оценка качества."""
    from apps.documents.models import DocumentQuality

    from . import quality

    counts = {"documents": 0, "reextracted": 0, "recovered": 0, "refetched": 0, "changed_score": 0}
    own = client is None
    client = client or PoliteClient(delay=incident.source.request_delay_seconds)
    try:
        for doc in incident_documents(incident).select_related("source").iterator(chunk_size=200):
            counts["documents"] += 1
            current = DocumentQuality.objects.filter(document=doc, is_current=True).first()
            before = current.score if current else None
            reextracted = reextract_document(doc)
            if reextracted:
                counts["reextracted"] += 1
                recovered, failed = recover_duplicates(doc, client)
                counts["recovered"] += recovered
                counts["recover_errors"] = counts.get("recover_errors", 0) + failed
                doc.refresh_from_db()
                current = quality.assess(doc)
            suspicious = current is not None and any(getattr(current, f) for f in REFETCH_FLAGS)
            if (
                suspicious
                and not reextracted
                and counts["refetched"] < MAX_REFETCH
                and doc.status == Document.Status.ACTIVE
            ):
                try:
                    outcome = recheck_document(doc, client)
                except Exception as exc:  # noqa: BLE001 — один документ не останавливает остальные
                    outcome = f"error: {type(exc).__name__}"
                counts["refetched"] += 1
                counts[outcome] = counts.get(outcome, 0) + 1
            if quality.assess(doc).score != before:
                counts["changed_score"] += 1
    finally:
        if own:
            client.close()
    return counts


@shared_task
def recheck_incident(incident_id: int) -> dict:
    """Перепроверка документов, собранных во время закрытого инцидента (Job «kvalita»)."""
    from apps.panel.models import Job

    from .models import SourceIncident

    incident = SourceIncident.objects.select_related("source").get(pk=incident_id)
    lock = f"ingestion:collect:{incident.source_id}"  # не мешать сбору того же источника
    if not cache.add(lock, "1", LOCK_TTL):
        recheck_incident.apply_async((incident_id,), countdown=300)
        return {"retry": incident_id}
    job = Job.objects.create(
        job_type=Job.Type.QUALITY, status=Job.Status.RUNNING, started_at=timezone.now(),
        params={"incident": incident.pk, "source": incident.source_id,
                "type": incident.incident_type},
    )  # fmt: skip
    try:
        # отметка до пересчёта: оценки документов окна больше не тянет вниз этот инцидент
        SourceIncident.objects.filter(pk=incident.pk).update(rechecked_at=timezone.now())
        counts = recheck_incident_documents(incident)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:1000]
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.FAILED, finished_at=timezone.now(), message=message
        )
        raise
    finally:
        cache.delete(lock)
    Job.objects.filter(pk=job.pk).update(
        status=Job.Status.SUCCEEDED, progress=1, finished_at=timezone.now(), result=counts,
        message=f"Po incidentu „{incident.get_incident_type_display()}“ ({incident.source}): "
        f"dokumentů {counts['documents']}, znovu extrahováno {counts['reextracted']}, "
        f"obnoveno ztracených {counts['recovered']}, znovu staženo {counts['refetched']}, "
        f"změněné hodnocení {counts['changed_score']}.",
    )  # fmt: skip
    if counts["changed_score"]:
        from apps.analytics.tasks import rebuild_aggregates

        rebuild_aggregates.delay()  # оценки качества меняют знаменатели
    return counts


@shared_task
def monitor_sources() -> dict:
    """Проверки мониторинга по всем активным источникам, где уже шёл сбор (README §4.2)."""
    from . import monitoring

    close_stale_runs()
    statuses = {}
    for source in Source.objects.filter(is_active=True, collection_runs__isnull=False).distinct():
        try:
            results = monitoring.monitor_source(source)
            statuses[source.pk] = [r.status for r in results]
        except Exception:  # noqa: BLE001 — один источник не останавливает остальные
            logger.exception("Monitoring %s selhal", source)
    return statuses


@shared_task
def check_golden_pages() -> dict:
    """Ежедневное перепарсивание контрольных страниц и сравнение с эталоном."""
    from . import monitoring

    now, statuses = timezone.now(), {}
    for source in monitoring.golden_sources():
        with PoliteClient(delay=source.request_delay_seconds) as client:
            result = monitoring.check_golden(source, now, client)
        if result is not None:
            monitoring.apply(source, result, now)
            statuses[source.pk] = result.status
    return statuses


@shared_task
def send_telegram_pending() -> int:
    from apps.panel import telegram

    return telegram.send_pending()
