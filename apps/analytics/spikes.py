"""Уведомления о резких всплесках у акторов (README §7.2 «Уведомления»).

Всплеск — недавняя неделя, где доля нарратива у актора выше собственной истории
(z ≥ spike-z-threshold, хотя бы один документ). Одна неделя × актор × нарратив — одно
уведомление: повторные пересчёты агрегатов не дублируют ленту.
"""

import datetime

from django.db.models import Max
from django.urls import reverse

from apps.narratives.models import NarrativeStatus
from apps.panel.defaults import get_setting
from apps.panel.models import Notification

from .models import WeeklyAggregate

RECENT_WEEKS = 2  # текущая и прошлая неделя: старые всплески в ленту не идут


def _key(row) -> str:
    return f"{row.actor_id}:{row.narrative_id}:{row.week.isoformat()}"


def notify_spikes() -> int:
    last = WeeklyAggregate.objects.aggregate(last=Max("week"))["last"]
    if last is None:
        return 0
    rows = (
        WeeklyAggregate.objects.filter(
            actor__isnull=False,
            min_quality=int(get_setting("analysis-min-quality")),
            narrative__status=NarrativeStatus.ACTIVE,
            week__gt=last - datetime.timedelta(weeks=RECENT_WEEKS),
            z_score__gte=float(get_setting("spike-z-threshold")),
            n_documents__gt=0,
        )
        .select_related("actor", "narrative")
        .order_by("-z_score")
    )
    known = set(
        Notification.objects.filter(notification_type=Notification.Type.SPIKE).values_list(
            "object_id", flat=True
        )
    )
    fresh = [row for row in rows if _key(row) not in known]
    for row in fresh:
        Notification.objects.create(
            notification_type=Notification.Type.SPIKE,
            level=Notification.Level.INFO,
            title=f"Výkyv: {row.actor} — {row.narrative.name}"[:300],
            text=f"Týden od {row.week:%-d. %-m. %Y}: {row.n_documents} z {row.n_total} dokumentů "
            f"(podíl {row.share:.0%}, z-skóre {row.z_score:.1f}).",
            url=reverse("dashboard:actor", args=[row.actor_id]),
            object_id=_key(row),
        )
    return len(fresh)
