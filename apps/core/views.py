from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.documents.models import Document
from apps.ingestion.models import CollectionRun

from .roles import can_annotate, can_view_dashboard
from .tasks import HEARTBEAT_MAX_AGE, heartbeat_age


@require_GET
def home(request):
    # разметчик без доступа к рабочей части сразу попадает в разметку
    if can_annotate(request.user) and not can_view_dashboard(request.user):
        return redirect("validation:index")
    return redirect("dashboard:index")


@never_cache
@require_GET
def healthz(request):
    """Health-check для Docker и мониторинга: БД доступна и pgvector установлен."""
    status = {"db": False, "pgvector": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cursor.fetchone()
        status["db"] = True
        status["pgvector"] = row is not None
    except Exception:  # noqa: BLE001 — health-check не должен падать
        pass
    try:
        status["cache"] = cache.set("core:healthz", "1", timeout=10) is not False
    except Exception:  # noqa: BLE001
        status["cache"] = False
    age = heartbeat_age()
    extra = {"heartbeat_age_s": round(age) if age is not None else None}
    last_run = (
        CollectionRun.objects.order_by("-started_at").values_list("started_at", flat=True).first()
        if status["db"]
        else None
    )
    extra["last_collection"] = last_run.isoformat() if last_run else None
    # Очередь обработки (этап 5): растущее число — воркер processor стоит.
    extra["processing_pending"] = (
        Document.objects.filter(processed_at__isnull=True).count() if status["db"] else None
    )
    if settings.HEALTHZ_CHECK_SCHEDULER:
        status["scheduler"] = age is not None and age < HEARTBEAT_MAX_AGE
    ok = all(status.values())
    return JsonResponse(
        {"status": "ok" if ok else "error", **status, **extra}, status=200 if ok else 503
    )
