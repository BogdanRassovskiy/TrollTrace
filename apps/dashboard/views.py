from django.views.generic import TemplateView


class DashboardIndexView(TemplateView):
    """Рабочая часть: дашборд акторов. Наполняется на Этапе 8."""

    template_name = "dashboard/index.html"
