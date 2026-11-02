from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Пока защита не настроена, в разработке всё открыто. Включить вход: OPEN_ACCESS=false.
OPEN_ACCESS = env.bool("OPEN_ACCESS", default=True)  # noqa: F405

# В разработке манифест статики не нужен (не требует collectstatic).
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
