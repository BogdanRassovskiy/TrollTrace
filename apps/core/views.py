from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
def home(request):
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
    ok = all(status.values())
    return JsonResponse({"status": "ok" if ok else "error", **status}, status=200 if ok else 503)
