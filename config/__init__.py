# Celery-приложение загружается вместе с Django, чтобы @shared_task использовали его.
from .celery import app as celery_app

__all__ = ("celery_app",)
