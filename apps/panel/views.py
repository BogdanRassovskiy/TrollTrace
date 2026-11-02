from django.contrib.contenttypes.models import ContentType
from django.urls import reverse_lazy
from django.views.generic import ListView, TemplateView

from apps.core.roles import ResearcherRequiredMixin

from .models import AuditLog

# Разделы панели исследователя (README §7.2): название, этап, ссылка (если раздел уже есть).
SECTIONS = [
    ("Акторы", "Этап 9", None),
    ("Источники и здоровье сбора", "Этап 3.1 / 9", None),
    ("Кодбук", "Этап 6 / 9", None),
    ("Настройки", "Этап 9", None),
    ("Валидация", "Этап 10", None),
    ("Задачи", "Этап 9", None),
    ("Уведомления", "Этап 3.1 / 9", None),
    ("Журнал аудита", "Готово", reverse_lazy("panel:audit")),
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
