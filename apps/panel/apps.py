from django.apps import AppConfig


class PanelConfig(AppConfig):
    name = "apps.panel"
    verbose_name = "Панель исследователя"

    def ready(self):
        from . import signals  # noqa: F401
