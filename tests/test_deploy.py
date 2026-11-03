"""Etapa 4: pulz Celery a /healthz/ pro monitoring serveru."""

import datetime

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.core.tasks import HEARTBEAT_KEY, heartbeat, heartbeat_age

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


def test_healthz_without_scheduler_check(client):
    data = client.get("/healthz/").json()
    assert data["status"] == "ok" and data["cache"] and "scheduler" not in data
    assert data["heartbeat_age_s"] is None and data["last_collection"] is None


def test_healthz_requires_fresh_heartbeat_in_prod(client, settings):
    settings.HEALTHZ_CHECK_SCHEDULER = True
    response = client.get("/healthz/")
    assert response.status_code == 503 and response.json()["scheduler"] is False

    heartbeat()
    response = client.get("/healthz/")
    assert response.status_code == 200 and response.json()["heartbeat_age_s"] == 0

    stale = timezone.now() - datetime.timedelta(minutes=20)
    cache.set(HEARTBEAT_KEY, stale.isoformat())
    assert client.get("/healthz/").status_code == 503


def test_heartbeat_pings_external_monitor(settings, monkeypatch):
    settings.HEARTBEAT_PING_URL = "https://hc-ping.example/uuid"
    calls = []
    monkeypatch.setattr("apps.core.tasks.httpx.get", lambda url, timeout: calls.append(url))
    heartbeat()
    assert calls == ["https://hc-ping.example/uuid"] and heartbeat_age() < 5
