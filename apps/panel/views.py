from django.views.generic import TemplateView

# Разделы панели исследователя (README §7.2). Страницы появляются по этапам.
SECTIONS = [
    ("Акторы", "Этап 9"),
    ("Источники и здоровье сбора", "Этап 3.1 / 9"),
    ("Кодбук", "Этап 6 / 9"),
    ("Настройки", "Этап 9"),
    ("Валидация", "Этап 10"),
    ("Задачи", "Этап 9"),
    ("Уведомления", "Этап 3.1 / 9"),
    ("Журнал аудита", "Этап 2 / 9"),
]


class PanelIndexView(TemplateView):
    """Панель исследователя. Доступ по роли появляется на Этапе 2."""

    template_name = "panel/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["sections"] = SECTIONS
        return context
