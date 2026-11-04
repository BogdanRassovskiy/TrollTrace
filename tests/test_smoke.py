"""Smoke-тесты каркаса (Этап 0): страницы открываются, БД с pgvector, Celery, миграции."""

from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.urls import reverse

from apps.core.tasks import ping

LOCAL_APPS = [
    "core",
    "actors",
    "documents",
    "narratives",
    "ingestion",
    "processing",
    "analytics",
    "dashboard",
    "panel",
    "validation",
]


def test_all_local_apps_installed():
    for label in LOCAL_APPS:
        assert apps.is_installed(f"apps.{label}"), label


def test_home_redirects_to_dashboard(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response.url == reverse("dashboard:index")


@pytest.mark.django_db
def test_dashboard_renders_with_echarts(observer_client):
    response = observer_client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "Pracovní část" in content
    assert "vendor/echarts.min.js" in content


@pytest.mark.django_db
def test_panel_renders_sections(researcher_client):
    response = researcher_client.get(reverse("panel:index"))
    assert response.status_code == 200
    assert "Auditní protokol" in response.content.decode()


@pytest.mark.django_db
def test_healthz_reports_db_and_pgvector(client):
    response = client.get(reverse("core:healthz"))
    assert response.status_code == 200
    data = response.json()
    assert (data["status"], data["db"], data["pgvector"]) == ("ok", True, True)
    assert data["processing_pending"] == 0


def test_celery_ping_task():
    assert ping.delay().get(timeout=5) == "pong"


@pytest.mark.django_db
def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)


def test_interface_is_czech_even_for_russian_browser(client, django_user_model):
    """Rozhraní je vždy česky, i když prohlížeč preferuje ruštinu (CLAUDE.md)."""
    django_user_model.objects.create_superuser("admin-cs", password="x")
    client.login(username="admin-cs", password="x")
    response = client.get("/admin/", HTTP_ACCEPT_LANGUAGE="ru,en;q=0.8")
    content = response.content.decode()
    assert '<html lang="cs"' in content and "Odhlásit se" in content


@pytest.mark.django_db
def test_stale_worker_code_is_detected(monkeypatch):
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    from apps.core import tasks
    from apps.panel.models import Notification

    tasks.remember_code_migrations()
    assert tasks.check_stale_code("worker") == []
    applied = sorted(MigrationRecorder(connection).applied_migrations())
    newest = next(m for m in reversed(applied) if m[0] == "documents")
    monkeypatch.setattr(tasks, "_KNOWN_MIGRATIONS", tasks._KNOWN_MIGRATIONS - {newest})

    assert tasks.check_stale_code("worker") == [f"{newest[0]}.{newest[1]}"]
    assert tasks.heartbeat() == "zastaralý kód"  # bez pulsu
    notification = Notification.objects.get()  # jen jednou za den
    assert notification.level == "critical" and "restartujte" in notification.title
