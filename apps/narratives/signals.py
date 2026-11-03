"""Новая версия нарратива → ретроспективная разметка архива (README §3)."""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import NarrativeStatus, NarrativeVersion


@receiver(post_save, sender=NarrativeVersion)
def schedule_relabel(sender, instance, created, **kwargs):
    if not created or instance.status != NarrativeStatus.ACTIVE:
        return

    def enqueue():
        from apps.panel.models import Job
        from apps.processing.tasks import relabel_narrative

        job = Job.objects.create(
            job_type=Job.Type.RELABEL,
            params={"narrative": instance.narrative.code, "version": instance.version},
            created_by=instance.changed_by,
        )
        result = relabel_narrative.delay(instance.narrative_id, job.pk)
        job.celery_task_id = result.id or ""
        job.save(update_fields=["celery_task_id"])

    transaction.on_commit(enqueue)
