"""Задачи обработки. Идут в очередь processing (отдельный воркер, модель грузится один раз)."""

from celery import shared_task
from django.core.cache import cache

from . import labeling
from .pipeline import process_pending

LOCK_KEY = "processing:lock"
LOCK_TTL = 30 * 60
TIME_BUDGET = 4 * 60  # beat запускает задачу каждые 2 минуты; длинная очередь — по частям


@shared_task
def process_pending_documents() -> dict:
    """Новые и изменённые документы проходят воронку (README §4, шаги 3–6)."""
    from apps.core.tasks import check_stale_code

    if check_stale_code("processor"):
        return {"skipped": "zastaralý kód: restartujte processor"}
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiné zpracování"}
    try:
        return process_pending(time_budget=TIME_BUDGET).as_dict()
    finally:
        cache.delete(LOCK_KEY)


LABEL_LOCK_KEY = "labeling:lock"
# Kratší než LOCK_TTL: úloha běží nejvýš TIME_BUDGET (+ jedno volání modelu), a když ji
# přeruší restart workeru, zámek nesmí blokovat frontu půl hodiny.
LABEL_LOCK_TTL = TIME_BUDGET + 6 * 60


@shared_task
def label_pending_decisions() -> dict:
    """Кандидаты разметки → LLM (README §4, шаг 7), в пределах месячного бюджета."""
    if not cache.add(LABEL_LOCK_KEY, "1", LABEL_LOCK_TTL):
        return {"skipped": "běží jiná anotace"}
    try:
        return labeling.label_pending(time_budget=TIME_BUDGET).as_dict()
    finally:
        cache.delete(LABEL_LOCK_KEY)


@shared_task
def relabel_narrative(narrative_id: int, job_id: int | None = None) -> dict:
    """Ретроразметка архива по новой версии нарратива (README §3)."""
    from django.utils import timezone

    from apps.narratives.models import Narrative
    from apps.panel.models import Job, Notification

    job = Job.objects.filter(pk=job_id).first() if job_id else None
    if job:
        Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, started_at=timezone.now())
    narrative = Narrative.objects.get(pk=narrative_id)
    try:
        queued = labeling.queue_narrative(narrative)
    except Exception as exc:
        if job:
            Job.objects.filter(pk=job.pk).update(
                status=Job.Status.FAILED, message=str(exc)[:1000], finished_at=timezone.now()
            )
        raise
    result = {"narrative": narrative.code, "version": narrative.current_version, "queued": queued}
    if job:
        Job.objects.filter(pk=job.pk).update(
            status=Job.Status.SUCCEEDED, progress=1, result=result, finished_at=timezone.now(),
            message=f"Do fronty anotace přidáno kandidátů: {queued}.",
        )  # fmt: skip
    Notification.objects.create(
        notification_type=Notification.Type.JOB,
        title=f"Zpětné anotování „{narrative.name}“ (v{narrative.current_version}): "
        f"{queued} kandidátů ve frontě",
        text="Kandidáty postupně posoudí model (úloha label_pending_decisions).",
    )
    return result


@shared_task
def mature_candidates() -> dict:
    """Дозревание кандидатов (novelty.mature) раз в сутки; под тем же замком, что и воронка:
    новое утверждение могло бы оживить кластер, который сейчас решается."""
    from apps.panel.defaults import get_setting

    from . import novelty

    budget = float(get_setting("labeling-monthly-budget-usd"))
    if labeling.month_spend() >= budget:
        return {"skipped": f"vyčerpán měsíční rozpočet {budget} USD"}
    if not cache.add(LOCK_KEY, "1", LOCK_TTL):
        return {"skipped": "běží jiné zpracování"}
    try:
        stats = novelty.mature()
    finally:
        cache.delete(LOCK_KEY)
    novelty.notify_maturation(stats)
    return stats
