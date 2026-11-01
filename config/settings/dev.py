from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# В разработке манифест статики не нужен (не требует collectstatic).
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
