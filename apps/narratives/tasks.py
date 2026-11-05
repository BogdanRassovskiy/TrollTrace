"""Фоновые задачи кодбука."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def propose_tree_task(topic_id: int, user_id: int | None = None) -> dict:
    """Návrh stromu pro téma modelem; výsledek — TreeDraft a upozornění v panelu."""
    from django.urls import reverse

    from apps.panel.models import Notification
    from apps.processing import llm

    from .models import Topic
    from .tree_draft import propose

    topic = Topic.objects.get(pk=topic_id)
    try:
        draft = propose(topic)
    except (llm.LLMError, ValueError) as exc:
        logger.warning("Návrh stromu pro %s selhal: %s", topic, exc)
        Notification.objects.create(
            notification_type=Notification.Type.JOB,
            level=Notification.Level.WARNING,
            title=f"Návrh stromu „{topic.name}“ se nepodařil"[:300],
            text=str(exc)[:1000],
            url=reverse("panel:tree"),
        )
        return {"topic": topic.code, "error": str(exc)[:300]}
    Notification.objects.create(
        notification_type=Notification.Type.JOB,
        title=f"Návrh stromu „{topic.name}“ je připraven"[:300],
        text=f"Nadnarativů: {len(draft.proposal['groups'])}. Zkontrolujte a použijte ho v panelu.",
        url=reverse("panel:tree_draft", args=[draft.pk]),
    )
    return {"topic": topic.code, "draft": draft.pk}
