"""Пересчёт агрегатов: инкрементально каждые 15 минут, полностью раз в сутки."""

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from . import aggregation, spikes

LOCK_KEY = "analytics:lock"
LOCK_TTL = 30 * 60
RETRY_SECONDS = 60


@shared_task
def update_aggregates() -> dict:
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiný přepočet"}
    try:
        result = aggregation.update_incremental().as_dict()
    finally:
        cache.delete(LOCK_KEY)
    result["spikes"] = spikes.notify_spikes()
    return result


@shared_task
def rebuild_aggregates(job_id: int | None = None) -> dict:
    """Полный пересчёт (страховка от изменений без отметки времени: периоды, статусы)."""
    from apps.panel.models import Job
    from apps.panel.tasks import finish_job

    job = Job.objects.filter(pk=job_id).first() if job_id else None
    if job_id and (
        job is None or job.job_type != Job.Type.AGGREGATE or job.status != Job.Status.PENDING
    ):
        # чужая или уже обработанная запись (отменена, другая БД с тем же брокером) — не трогаем
        return {"skipped": job_id}
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        if job is None:
            return {"skipped": "běží jiný přepočet"}
        # Задача из панели не теряется: ждёт окончания другого пересчёта.
        Job.objects.filter(pk=job.pk).update(message="Čeká na dokončení jiného přepočtu.")
        rebuild_aggregates.apply_async((job.pk,), countdown=RETRY_SECONDS)
        return {"retry": job.pk}
    if job is None:
        job = Job.objects.create(job_type=Job.Type.AGGREGATE, params={"full": True})
    Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, started_at=timezone.now())
    started = timezone.now()
    try:
        stats = aggregation.update(full=True)
    except Exception as exc:
        finish_job(job, Job.Status.FAILED, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        cache.delete(LOCK_KEY)
    cache.set(aggregation.WATERMARK_KEY, started.isoformat(), None)
    result = stats.as_dict()
    result["spikes"] = spikes.notify_spikes()
    message = f"Řádků aktérů {stats.actor_rows}, skupin {stats.group_rows}."
    if job.created_by_id or job.params.get("reason"):
        finish_job(job, Job.Status.SUCCEEDED, message, result)  # ручная задача → уведомление
    else:
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.SUCCEEDED,
            progress=1,
            result=result,
            finished_at=timezone.now(),
            message=message,
        )
    return result
