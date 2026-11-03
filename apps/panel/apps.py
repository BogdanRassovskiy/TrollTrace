from django.apps import AppConfig


class PanelConfig(AppConfig):
    name = "apps.panel"
    verbose_name = "Panel výzkumníka"

    def ready(self):
        from . import signals  # noqa: F401
