"""Общие настройки. Окружение-специфичные значения берутся из переменных окружения (.env)."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# --- Приложения ---------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

# Порядок отражает зависимости: core → данные → обработка → интерфейс.
LOCAL_APPS = [
    "apps.core",  # общие утилиты, базовые модели, pgvector, health-check
    "apps.actors",  # Source, Actor, ActorInclusionPeriod, ActorAccount
    "apps.documents",  # Document, DocumentVersion, DocumentQuality, ReuseLink
    "apps.narratives",  # Topic, Narrative, NarrativeVersion, Label, Debunk, Candidate
    "apps.ingestion",  # коллекторы по слоям A–E, мониторинг сбора
    "apps.processing",  # воронка: релевантность, эмбеддинги, MinHash/LSH, kNN
    "apps.analytics",  # WeeklyAggregate, API для графиков
    "apps.dashboard",  # рабочая часть (README §7.1)
    "apps.panel",  # панель исследователя (README §7.2)
    "apps.validation",  # ручная разметка и согласие разметчиков
    "apps.institutions",  # слой I: институты безопасности, события, event study
]

INSTALLED_APPS = DJANGO_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.OpenAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.project",
            ],
        },
    },
]

# --- База данных --------------------------------------------------------------

# PostgreSQL обязателен: pgvector нужен для эмбеддингов (README §4, шаг 4).
DATABASES = {
    "default": env.db(
        "DATABASE_URL", default="postgres://trolltrace:trolltrace@localhost:5432/trolltrace"
    ),
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Аутентификация -----------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Временно: доступ без входа для всех (см. apps/core/middleware.py). В prod — выключено.
OPEN_ACCESS = env.bool("OPEN_ACCESS", default=False)

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "core:home"  # главная разводит по ролям (разметчик → разметка)
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 60 * 60 * 12  # рабочий день; база закрытая (README §6)

# --- Локализация --------------------------------------------------------------

LANGUAGE_CODE = "cs"
# Rozhraní je jen česky (CLAUDE.md): jazyk se nebere z prohlížeče.
LANGUAGES = [("cs", "Čeština")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Prague"
USE_I18N = True
USE_TZ = True  # в БД всё хранится в UTC

# --- Статика ------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- Celery -------------------------------------------------------------------

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60  # жёсткий предел; длинные задачи (ретроразметка) бьются на чанки
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # тяжёлые задачи не накапливаются на одном воркере
CELERY_TASK_ACKS_LATE = True
# Расписание (README §4.1, §10 «сбор запускать как можно раньше»). Интервал сбора каждого
# источника — Source.collect_interval_minutes; beat лишь проверяет, кому пора.
CELERY_BEAT_SCHEDULE = {
    "collect-due-sources": {
        "task": "apps.ingestion.tasks.collect_due_sources",
        "schedule": 5 * 60,
    },
    "recheck-documents": {
        "task": "apps.ingestion.tasks.recheck_documents",
        "schedule": 60 * 60,
    },
    "sync-psp-deputies": {
        "task": "apps.ingestion.tasks.sync_psp_deputies",
        "schedule": 7 * 24 * 60 * 60,
    },
    # Мониторинг (README §4.2): проверки раз в час, контрольные страницы раз в сутки.
    "monitor-sources": {
        "task": "apps.ingestion.tasks.monitor_sources",
        "schedule": 60 * 60,
    },
    "check-golden-pages": {
        "task": "apps.ingestion.tasks.check_golden_pages",
        "schedule": 24 * 60 * 60,
    },
    # Пульс: проходит через beat и worker; без него /healthz/ в проде отвечает 503.
    "heartbeat": {
        "task": "apps.core.tasks.heartbeat",
        "schedule": 5 * 60,
    },
    "send-telegram-pending": {
        "task": "apps.ingestion.tasks.send_telegram_pending",
        "schedule": 10 * 60,
    },
    # Воронка обработки (README §4, шаги 3–6): новые документы, отдельный воркер processor.
    "process-pending-documents": {
        "task": "apps.processing.tasks.process_pending_documents",
        "schedule": 2 * 60,
    },
    # Разметка нарративов (README §4, шаг 7): кандидаты → LLM, по частям.
    "label-pending-decisions": {
        "task": "apps.processing.tasks.label_pending_decisions",
        "schedule": 10 * 60,
    },
    # Агрегация (README §4, шаг 8): инкрементально, полный пересчёт раз в сутки.
    "update-aggregates": {
        "task": "apps.analytics.tasks.update_aggregates",
        "schedule": 15 * 60,
    },
    "rebuild-aggregates": {
        "task": "apps.analytics.tasks.rebuild_aggregates",
        "schedule": 24 * 60 * 60,
    },
}
# Эмбеддинги считает только воркер очереди processing: модель (~1 ГБ) грузится в нём одном.
CELERY_TASK_ROUTES = {"apps.processing.tasks.*": {"queue": "processing"}}

# Кэш на Redis: robots.txt и блокировки «один запуск сбора на источник» общие для воркеров.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://localhost:6379/0"),
        "KEY_PREFIX": "trolltrace",
    }
}

# --- Проект -------------------------------------------------------------------

TROLLTRACE = {
    "VERSION": "0.1.0",
    "CRAWLER_USER_AGENT": env(
        "CRAWLER_USER_AGENT",
        default="TrollTraceResearchBot/0.1 (+mailto:change-me@example.org)",
    ),
}

# Уведомления мониторинга сбора в Telegram (README §4.2). Пусто — только панель.
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", default="")
# Внешний «пульс» (например, healthchecks.io): пингуется каждые 5 минут из Celery.
# Если сбор встал (умер worker или beat), сервис пришлёт тревогу сам.
HEARTBEAT_PING_URL = env("HEARTBEAT_PING_URL", default="")
# /healthz/ проверяет и свежесть пульса Celery (в проде; в dev beat может быть выключен).
HEALTHZ_CHECK_SCHEDULER = env.bool("HEALTHZ_CHECK_SCHEDULER", default=False)
# Эмбеддинги: sentence-transformers (модель из настройки embedding-model) или hashing (тесты).
EMBEDDING_BACKEND = env("EMBEDDING_BACKEND", default="sentence-transformers")
# Разметка нарративов (README §4, шаг 7): LLM через OpenRouter. Пусто — кандидаты ждут.
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
# Адрес сайта для ссылок в Telegram-сообщениях.
SITE_URL = env("SITE_URL", default="http://localhost:8000")
# Доступ через прокси с HTTPS (tailscale serve, nginx): адрес должен быть в списке, иначе
# Django отклонит формы (проверка CSRF сравнивает Origin со схемой запроса).
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# --- Логирование --------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
