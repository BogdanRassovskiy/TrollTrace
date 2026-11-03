from django.apps import AppConfig, apps
from django.db.models.signals import post_migrate


def _ensure_roles(app_config, **kwargs):
    # Права моделей создаются в post_migrate каждого приложения — ждём последнего.
    if app_config is list(apps.get_app_configs())[-1]:
        from .roles import ensure_roles

        ensure_roles()


class CoreConfig(AppConfig):
    name = "apps.core"
    verbose_name = "Jádro"

    def ready(self):
        post_migrate.connect(_ensure_roles, dispatch_uid="trolltrace_ensure_roles")
