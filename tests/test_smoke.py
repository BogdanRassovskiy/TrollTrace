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
    assert "Рабочая часть" in content
    assert "vendor/echarts.min.js" in content


@pytest.mark.django_db
def test_panel_renders_sections(researcher_client):
    response = researcher_client.get(reverse("panel:index"))
    assert response.status_code == 200
    assert "Журнал аудита" in response.content.decode()


@pytest.mark.django_db
def test_healthz_reports_db_and_pgvector(client):
    response = client.get(reverse("core:healthz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "pgvector": True}


def test_celery_ping_task():
    assert ping.delay().get(timeout=5) == "pong"


@pytest.mark.django_db
def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
