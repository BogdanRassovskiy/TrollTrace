import tempfile
from pathlib import Path

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key"
OPEN_ACCESS = False  # тесты проверяют роли; открытый режим — отдельным тестом
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

# Задачи Celery выполняются синхронно — тестам не нужен брокер.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# STATIC_ROOT в тестах не собирается — whitenoise ищет файлы через finders.
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="trolltrace-media-"))  # фото акторов в тестах
