from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Пока защита не настроена, в разработке всё открыто. Включить вход: OPEN_ACCESS=false.
OPEN_ACCESS = env.bool("OPEN_ACCESS", default=True)  # noqa: F405

# В разработке манифест статики не нужен (не требует collectstatic).
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

# Статику в разработке отдаёт WhiteNoise, а не runserver: тот не ставит Cache-Control, и браузер
# эвристически держит старый JS при новом HTML (кнопки «молча» не работают). WhiteNoise при
# DEBUG ищет файлы через finders, перечитывает их на лету и отдаёт с max-age=0 — браузер
# каждый раз перепроверяет файл (304, если не менялся).
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]  # noqa: F405
WHITENOISE_MAX_AGE = 0
