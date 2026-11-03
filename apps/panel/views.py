import datetime

from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, Max, Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.core.roles import ResearcherRequiredMixin

from .models import AuditLog, Notification

# Разделы панели исследователя (README §7.2): название, этап, ссылка (если раздел уже есть).
SECTIONS = [
    ("Aktéři", "Etapa 9", None),
    ("Zdroje a stav sběru", "Hotovo (3.1)", reverse_lazy("panel:sources")),
    ("Kódová kniha", "Etapa 6 / 9", None),
    ("Nastavení", "Etapa 9", None),
    ("Validace", "Etapa 10", None),
    ("Úlohy", "Etapa 9", None),
    ("Upozornění", "Hotovo (3.1)", reverse_lazy("panel:notifications")),
    ("Auditní protokol", "Hotovo", reverse_lazy("panel:audit")),
]


class PanelIndexView(ResearcherRequiredMixin, TemplateView):
    """Панель исследователя. Только для роли «исследователь»."""

    template_name = "panel/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["sections"] = SECTIONS
        return context


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
