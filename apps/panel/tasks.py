"""Фоновые задачи панели: ретроспективный сбор по новому или вновь включённому актору."""

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from .models import Job, Notification

RETRY_SECONDS = 60


def finish_job(job: Job, status: str, message: str, result: dict | None = None) -> None:
    """Закрывает Job и пишет уведомление о завершении долгой задачи (README §7.2)."""
    Job.objects.filter(pk=job.pk).update(
        status=status,
        message=message[:1000],
        result=result or {},
        finished_at=timezone.now(),
        progress=1 if status == Job.Status.SUCCEEDED else job.progress,
    )
    failed = status == Job.Status.FAILED
    Notification.objects.create(
        notification_type=Notification.Type.JOB,
        level=Notification.Level.WARNING if failed else Notification.Level.INFO,
        title=f"{job.get_job_type_display()} #{job.pk}: {'chyba' if failed else 'hotovo'}",
        text=message,
        url=f"/panel/ulohy/?job={job.pk}",
        recipient=job.created_by,
    )


@shared_task
def retro_collect(job_id: int) -> dict:
    """Сбор по источникам актора «насколько позволяет источник»: коллектор сам читает
    историю (backfill_pages, sitemap) при первом запуске. Параллельный сбор того же
    источника не запускается — задача повторится позже."""
    from apps.actors.models import Source
    from apps.ingestion.tasks import LOCK_TTL, run_collector

    job = Job.objects.get(pk=job_id)
    if job.job_type != Job.Type.RETRO_COLLECT or job.status != Job.Status.PENDING:
        # чужая или уже обработанная запись (отменена, другая БД с тем же брокером) — не трогаем
        return {"skipped": job.pk}
    Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, started_at=timezone.now())
    done = dict(job.result.get("runs", {}))
    sources = [s for s in job.params.get("sources", []) if str(s) not in done]
    try:
        for index, source_id in enumerate(sources):
            lock = f"ingestion:collect:{source_id}"
            if not cache.add(lock, "1", LOCK_TTL):
                Job.objects.filter(pk=job.pk).update(
                    status=Job.Status.PENDING,
                    message="Zdroj se právě sbírá, opakuji později.",
                    result={"runs": done},
                )
                retro_collect.apply_async((job_id,), countdown=RETRY_SECONDS)
                return {"retry": source_id}
            try:
                run = run_collector(Source.objects.get(pk=source_id))
            finally:
                cache.delete(lock)
            done[str(source_id)] = {"run": run.pk, "status": run.status, "new": run.new}
            Job.objects.filter(pk=job.pk).update(
                progress=(index + 1) / len(sources), result={"runs": done}
            )
    except Exception as exc:
        finish_job(job, Job.Status.FAILED, f"{type(exc).__name__}: {exc}", {"runs": done})
        raise
    new = sum(r["new"] for r in done.values())
    finish_job(
        job,
        Job.Status.SUCCEEDED,
        f"Retrospektivní sběr: zdrojů {len(done)}, nových dokumentů {new}. Dokumenty "
        "projdou zpracováním a do agregátů se dostanou při dalším přepočtu.",
        {"runs": done},
    )
    return {"runs": done}
