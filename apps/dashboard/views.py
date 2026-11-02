from django.views.generic import TemplateView

from apps.core.roles import ViewerRequiredMixin


class DashboardIndexView(ViewerRequiredMixin, TemplateView):
    """Рабочая часть: дашборд акторов. Наполняется на Этапе 8."""

    template_name = "dashboard/index.html"
