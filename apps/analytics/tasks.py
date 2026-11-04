"""Пересчёт агрегатов: инкрементально каждые 15 минут, полностью раз в сутки."""

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from . import aggregation

LOCK_KEY = "analytics:lock"
LOCK_TTL = 30 * 60


@shared_task
def update_aggregates() -> dict:
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiný přepočet"}
    try:
        return aggregation.update_incremental().as_dict()
    finally:
        cache.delete(LOCK_KEY)


@shared_task
def rebuild_aggregates(job_id: int | None = None) -> dict:
    """Полный пересчёт (страховка от изменений без отметки времени: периоды, статусы)."""
    from apps.panel.models import Job

    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiný přepočet"}
    job = Job.objects.filter(pk=job_id).first() if job_id else None
    if job is None:
        job = Job.objects.create(job_type=Job.Type.AGGREGATE, params={"full": True})
    Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, started_at=timezone.now())
    started = timezone.now()
    try:
        stats = aggregation.update(full=True)
    except Exception as exc:
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.FAILED, message=str(exc)[:1000], finished_at=timezone.now()
        )
        raise
    finally:
        cache.delete(LOCK_KEY)
    cache.set(aggregation.WATERMARK_KEY, started.isoformat(), None)
    result = stats.as_dict()
    Job.objects.filter(pk=job.pk).update(
        status=Job.Status.SUCCEEDED, progress=1, result=result, finished_at=timezone.now(),
        message=f"Řádků aktérů {stats.actor_rows}, skupin {stats.group_rows}.",
    )  # fmt: skip
    return result
