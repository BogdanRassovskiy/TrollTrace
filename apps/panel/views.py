import datetime

from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views import View
from django.views.generic import ListView, RedirectView, TemplateView

from apps.core.roles import ResearcherRequiredMixin

from .models import AuditLog, Notification


class PanelIndexView(ResearcherRequiredMixin, RedirectView):
    """Вход в панель исследователя — сразу первая вкладка «Aktéři» (вкладки есть на всех
    страницах панели, отдельная страница-плитка не нужна)."""

    pattern_name = "panel:actors"


class AuditLogView(ResearcherRequiredMixin, ListView):
    """Журнал аудита: фильтры по пользователю, действию, типу объекта и поиск по объекту."""

    template_name = "panel/audit_log.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_queryset(self):
        qs = AuditLog.objects.select_related("content_type").order_by("-timestamp", "-pk")
        params = self.request.GET
        if params.get("user"):
            qs = qs.filter(username=params["user"])
        if params.get("action"):
            qs = qs.filter(action=params["action"])
        if params.get("type"):
            qs = qs.filter(content_type_id=params["type"])
        if params.get("object_id"):
            qs = qs.filter(object_id=params["object_id"])
        if params.get("q"):
            qs = qs.filter(object_repr__icontains=params["q"])
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.copy()
        query.pop("page", None)
        context.update(
            actions=AuditLog.Action.choices,
            usernames=AuditLog.objects.exclude(username="")
            .order_by("username")
            .values_list("username", flat=True)
            .distinct(),
            content_types=ContentType.objects.filter(
                pk__in=AuditLog.objects.exclude(content_type=None).values("content_type")
            ).order_by("app_label", "model"),
            filters=self.request.GET,
            querystring=query.urlencode(),
        )
        return context


class SourcesHealthView(ResearcherRequiredMixin, TemplateView):
    """Источники: статус здоровья, объём и средняя оценка за сутки, открытые инциденты (§4.2)."""

    template_name = "panel/sources.html"

    def get_context_data(self, **kwargs):
        from apps.actors.models import Source
        from apps.ingestion.models import SourceIncident

        context = super().get_context_data(**kwargs)
        day_ago = timezone.now() - datetime.timedelta(days=1)
        sources = Source.objects.annotate(
            docs_day=Count(
                "documents", filter=Q(documents__collected_at__gte=day_ago), distinct=True
            ),
            docs_total=Count("documents", distinct=True),
            last_run=Max("collection_runs__started_at"),
            mean_quality=Avg(
                "documents__quality_scores__score",
                filter=Q(
                    documents__quality_scores__is_current=True,
                    documents__collected_at__gte=day_ago,
                ),
            ),
        ).order_by("layer", "name")
        open_incidents = SourceIncident.objects.filter(ended_at__isnull=True)
        by_source: dict[int, list] = {}
        for incident in open_incidents:
            by_source.setdefault(incident.source_id, []).append(incident)
        rows = []
        for source in sources:
            incidents = by_source.get(source.pk, [])
            levels = {i.level for i in incidents}
            health = "red" if "critical" in levels else "yellow" if levels else "green"
            rows.append((source, health, incidents))
        context["rows"] = rows
        context["recent_incidents"] = SourceIncident.objects.select_related("source")[:30]
        context["mix"] = {period: sources_mix(period) for period in MIX_PERIODS}
        return context


CHART_DAYS = 30


def _chart_window(days: int):
    tz = timezone.get_current_timezone()
    start = timezone.localdate() - datetime.timedelta(days=days - 1)
    since = datetime.datetime.combine(start, datetime.time.min, tzinfo=tz)
    dates = [start + datetime.timedelta(days=i) for i in range(days)]
    return tz, since, dates


# Периоды общей гистограммы источников: сколько последних дней / недель / месяцев показывать.
MIX_PERIODS = {"day": 30, "week": 26, "month": 12}


def _mix_buckets(period: str, count: int) -> list[datetime.date]:
    """Начала последних `count` периодов (день; понедельник недели; 1-е число месяца)."""
    today = timezone.localdate()
    if period == "day":
        return [today - datetime.timedelta(days=count - 1 - i) for i in range(count)]
    if period == "week":
        monday = today - datetime.timedelta(days=today.weekday())
        return [monday - datetime.timedelta(weeks=count - 1 - i) for i in range(count)]
    months = today.year * 12 + today.month - 1
    return [
        datetime.date((m := months - (count - 1 - i)) // 12, m % 12 + 1, 1) for i in range(count)
    ]


def _mix_labels(period: str, start: datetime.date) -> tuple[str, str]:
    """Короткая подпись оси и полное название периода для подсказки."""
    if period == "day":
        return start.strftime("%d.%m."), date_format(start, "j. n. Y")
    if period == "week":
        end = start + datetime.timedelta(days=6)
        return (
            start.strftime("%d.%m."),
            f"týden {date_format(start, 'j. n.')} – {date_format(end, 'j. n. Y')}",
        )
    return start.strftime("%m/%Y"), date_format(start, "F Y")


def sources_mix(period: str = "day") -> dict:
    """Состав сбора по периодам для общей гистограммы источников: сколько документов дал
    каждый источник за день / неделю / месяц (по collected_at) и средняя оценка их качества.

    Источники без документов за окно не попадают в ряд, чтобы не засорять легенду.
    """
    from apps.actors.models import Source
    from apps.documents.models import Document, DocumentQuality

    trunc = {"day": TruncDate, "week": TruncWeek, "month": TruncMonth}[period]
    count = MIX_PERIODS[period]
    tz = timezone.get_current_timezone()
    buckets = _mix_buckets(period, count)
    since = datetime.datetime.combine(buckets[0], datetime.time.min, tzinfo=tz)
    index = {d: i for i, d in enumerate(buckets)}

    def bucket(value) -> int:
        # TruncWeek/TruncMonth над DateTimeField возвращают datetime в поясе проекта.
        if isinstance(value, datetime.datetime):
            value = timezone.localtime(value, tz).date()
        return index[value]

    counts: dict[int, list[int]] = {}
    rows = (
        Document.objects.filter(collected_at__gte=since)
        .annotate(p=trunc("collected_at", tzinfo=tz))
        .values("source_id", "p")
        .annotate(n=Count("id"))
    )
    for row in rows:
        counts.setdefault(row["source_id"], [0] * count)[bucket(row["p"])] = row["n"]

    quality: dict[int, list] = {pk: [None] * count for pk in counts}
    rows = (
        DocumentQuality.objects.filter(is_current=True, document__collected_at__gte=since)
        .annotate(p=trunc("document__collected_at", tzinfo=tz))
        .values("document__source_id", "p")
        .annotate(mean=Avg("score"))
    )
    for row in rows:
        series = quality.get(row["document__source_id"])
        if series is not None:
            series[bucket(row["p"])] = round(row["mean"], 2)

    labels = [_mix_labels(period, b) for b in buckets]
    sources = Source.objects.filter(pk__in=counts).order_by("layer", "name")
    return {
        "dates": [b.isoformat() for b in buckets],
        "labels": [short for short, _ in labels],
        "titles": [full for _, full in labels],
        "totals": [sum(c[i] for c in counts.values()) for i in range(count)],
        "sources": [
            {
                "id": s.pk,
                "name": s.name,
                "layer": s.layer,
                "url": reverse("panel:source_detail", args=[s.pk]),
                "counts": counts[s.pk],
                "quality": quality[s.pk],
            }
            for s in sources
        ],
    }


def daily_series(source, days: int = CHART_DAYS) -> dict:
    """Ряды по дням для графиков источника (README §4.2, §4.3): объём и оценка качества.

    День — по времени сбора (collected_at) в часовом поясе проекта: именно оно показывает,
    работает ли сбор; дата публикации для этого не годится.
    """
    from apps.documents.models import Document, DocumentQuality

    tz, since, dates = _chart_window(days)
    index = {d: i for i, d in enumerate(dates)}

    volume = [0] * days
    in_scope = [0] * days
    rows = (
        Document.objects.filter(source=source, collected_at__gte=since)
        .annotate(day=TruncDate("collected_at", tzinfo=tz))
        .values("day")
        .annotate(n=Count("id"), scoped=Count("id", filter=~Q(in_scope=False)))
    )
    for row in rows:
        volume[index[row["day"]]] = row["n"]
        in_scope[index[row["day"]]] = row["scoped"]

    scores = {score: [0] * days for score in range(1, 6)}
    totals = [0] * days
    sums = [0] * days
    rows = (
        DocumentQuality.objects.filter(
            is_current=True, document__source=source, document__collected_at__gte=since
        )
        .annotate(day=TruncDate("document__collected_at", tzinfo=tz))
        .values("day", "score")
        .annotate(n=Count("id"))
    )
    for row in rows:
        i = index[row["day"]]
        scores[row["score"]][i] = row["n"]
        totals[i] += row["n"]
        sums[i] += row["n"] * row["score"]
    mean = [round(sums[i] / totals[i], 2) if totals[i] else None for i in range(days)]
    return {
        "dates": [d.isoformat() for d in dates],
        "volume": volume,
        "in_scope": in_scope,
        "mean_quality": mean,
        "scores": {str(k): v for k, v in scores.items()},
    }


class SourceDetailView(ResearcherRequiredMixin, TemplateView):
    """Источник: графики объёма и качества по дням, инциденты, проверки, контрольные страницы."""

    template_name = "panel/source_detail.html"

    def get_context_data(self, **kwargs):
        from apps.actors.models import Source
        from apps.ingestion import monitoring

        context = super().get_context_data(**kwargs)
        source = get_object_or_404(Source, pk=kwargs["pk"])
        context.update(
            source=source,
            health=monitoring.health(source),
            series=daily_series(source),
            incidents=source.incidents.order_by("-started_at")[:50],
            checks=source.health_checks.order_by("-checked_at")[:20],
            golden_pages=source.golden_pages.all(),
            feeds=source.feeds.all(),
            runs=source.collection_runs.order_by("-started_at")[:10],
        )
        return context


class NotificationListView(ResearcherRequiredMixin, ListView):
    template_name = "panel/notifications.html"
    context_object_name = "notifications"
    paginate_by = 50

    def get_queryset(self):
        qs = Notification.objects.filter(
            Q(recipient=None) | Q(recipient=self.request.user)
        ).order_by("-created_at")
        if self.request.GET.get("unread"):
            qs = qs.filter(is_read=False)
        return qs


class NotificationMarkReadView(ResearcherRequiredMixin, View):
    def post(self, request):
        Notification.objects.filter(
            Q(recipient=None) | Q(recipient=request.user), is_read=False
        ).update(is_read=True, read_at=timezone.now())
        return redirect("panel:notifications")
