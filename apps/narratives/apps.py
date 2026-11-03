from django.apps import AppConfig


class NarrativesConfig(AppConfig):
    name = "apps.narratives"
    verbose_name = "Narativy a kódová kniha"

    def ready(self):
        from . import signals  # noqa: F401
