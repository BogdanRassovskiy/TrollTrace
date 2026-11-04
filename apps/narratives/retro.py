"""Ручная ретроразметка архива по текущей версии нарратива (README §3).

Автоматически не запускается: нарративы возникают постоянно, а прогон архива на каждый
дорог. Нужна, например, для нарратива из литературы или отчёта, добавленного задним числом,
или после существенного расширения определения.
"""

from django.db import transaction


def start_relabel(narrative, user=None):
    """Job «Zpětné anotování» + задача relabel_narrative после коммита транзакции."""
    from apps.panel.models import Job
    from apps.processing.tasks import relabel_narrative

    job = Job.objects.create(
        job_type=Job.Type.RELABEL,
        params={"narrative": narrative.code, "version": narrative.current_version},
        created_by=user,
    )

    def enqueue():
        result = relabel_narrative.delay(narrative.pk, job.pk)
        Job.objects.filter(pk=job.pk).update(celery_task_id=result.id or "")

    transaction.on_commit(enqueue)
    return job
