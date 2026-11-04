import os

from celery import Celery
from celery.signals import worker_init, worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("trolltrace")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_ready.connect
def _first_heartbeat(**kwargs):
    """Пульс сразу при старте: beat запускает задачи только через полный интервал."""
    from apps.core.tasks import heartbeat

    heartbeat.delay()


@worker_init.connect
def _remember_code(**kwargs):
    """Снимок миграций кода до форка пула: по нему пульс узнаёт устаревший код."""
    import django

    django.setup()
    from apps.core.tasks import remember_code_migrations

    remember_code_migrations()
