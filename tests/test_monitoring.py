"""Этап 3.1: оценка качества документа и мониторинг сбора."""

import datetime
import json

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.documents.models import Document, DocumentQuality
from apps.ingestion import monitoring, quality
from apps.ingestion.models import CollectionRun, GoldenPage, SourceHealthCheck, SourceIncident
from apps.ingestion.store import Item, Outcome, save_item
from apps.panel import telegram
from apps.panel.models import Notification

pytestmark = pytest.mark.django_db

CZECH = (
    "Vláda na svém dnešním jednání schválila návrh rozpočtu, který podle ministra financí "
    "počítá se schodkem. Opozice je proti a také podle ekonomů není jasné, jak se bude "
    "vyvíjet dluh. Návrh je nyní ve Sněmovně, kde ho čeká první čtení a debata. "
) * 3
NOW = timezone.now()


@pytest.fixture
def source():
    source = Source.objects.create(
        name="Zprávy", layer=Layer.MAINSTREAM, platform=Platform.WEB, url="https://z.example/",
        expected_language="cs", min_text_length=300,
    )  # fmt: skip
    actor = Actor.objects.create(name="Zprávy", actor_type="media", layer=Layer.MAINSTREAM)
    ActorAccount.objects.create(actor=actor, source=source, account_id="")
    return source


def save(source, n, **kwargs):
    fields = {
        "url": f"https://z.example/a/{n}",
        "title": f"Titulek {n}",
        "text": CZECH + f" Článek {n}.",
        "published_at": NOW - datetime.timedelta(hours=1),
        "date_source": Document.DateSource.PAGE,
        "rubric": "Domácí",
        "language": "cs",
        "raw_html": "<html>" + "x" * 5000 + "</html>",
        **kwargs,
    }
    return save_item(source, source.accounts.get().actor, Item(**fields))


def current(doc):
    return doc.quality_scores.get(is_current=True)


# --- Оценка качества ----------------------------------------------------------------------


class TestQualityScore:
    def test_clean_document_is_5(self, source):
        _, doc = save(source, 1)
        q = current(doc)
        assert q.score == 5 and q.quality_rules_version == quality.RULES_VERSION
        assert q.has_title and q.date_plausible and q.text_ok and q.lang_ok

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"date_source": Document.DateSource.FEED}, 4),
            ({"rubric": ""}, 4),
            ({"published_at": None, "date_source": Document.DateSource.NONE}, 3),
            ({"published_at": NOW + datetime.timedelta(days=2)}, 3),
            ({"text": CZECH[:120]}, 3),  # заметно короче нормы
            ({"text": "Jen pár slov."}, 1),
            ({"text": "Ověřte, že nejste robot. " + CZECH[:200]}, 1),
            (
                {
                    "text": "Это текст на русском языке, и он не совпадает с ожидаемым. " * 8,
                    "language": "",
                },
                2,
            ),  # fmt: skip
            ({"published_at": None, "date_source": "none", "text": CZECH[:120]}, 2),
        ],
    )
    def test_rules(self, source, kwargs, expected):
        _, doc = save(source, 1, **kwargs)
        assert current(doc).score == expected

    def test_reassessment_uses_incident_at_extraction_time(self, source):
        """Инцидент, открытый после сбора, не снижает оценку старого документа задним числом."""
        _, doc = save(source, 1)
        SourceIncident.objects.create(
            source=source, incident_type="http_errors", level="warning",
            started_at=timezone.now() + datetime.timedelta(minutes=1),
        )  # fmt: skip
        assert quality.assess(doc).score == 5
        assert quality.open_incident_level(source) == "warning"

    def test_open_incident_lowers_score(self, source):
        SourceIncident.objects.create(
            source=source, incident_type="low_volume", level="warning", started_at=NOW
        )
        _, doc = save(source, 1)
        assert current(doc).score == 3 and current(doc).incident_level == "warning"

    def test_short_speech_is_not_garbage(self):
        psp = Source.objects.create(
            name="PSP", layer=Layer.PARLIAMENT, platform=Platform.PARLIAMENT,
            url="https://psp.example/", min_text_length=50, expected_language="cs",
        )  # fmt: skip
        actor = Actor.objects.create(name="Poslanec", actor_type="politician", layer="E")
        _, doc = save_item(psp, actor, Item(url="https://psp.example/s#r1", text="Nesouhlasím.",
                                            published_at=NOW - datetime.timedelta(days=400),
                                            date_source="page", external_id="s#r1"))  # fmt: skip
        q = current(doc)
        assert not q.text_empty and q.date_plausible and q.score == 4  # коротко, но не мусор

    def test_new_version_rescored_and_history_kept(self, source):
        _, doc = save(source, 1)
        outcome, _ = save(source, 1, text="Přijmout vše cookies")
        assert outcome == Outcome.VERSION
        assert doc.quality_scores.count() == 2 and current(doc).score == 1

    def test_duplicate_text_flags_original(self, source):
        _, original = save(source, 1)
        outcome, _ = save(source, 2, text=original.text)
        assert outcome == Outcome.DUPLICATE
        original.refresh_from_db()
        assert original.meta["duplicate_urls"] == ["https://z.example/a/2"]
        assert current(original).boilerplate_dup and current(original).score == 1

    def test_detect_language(self):
        assert quality.detect_language(CZECH) == "cs"
        assert (
            quality.detect_language("Toto je text, ktorý sa píše po slovensky a je pre nás " * 5)
            == "sk"
        )
        assert quality.detect_language("krátký") == ""


# --- Мониторинг ---------------------------------------------------------------------------


def backdate(doc, days):
    Document.objects.filter(pk=doc.pk).update(
        collected_at=NOW - datetime.timedelta(days=days, hours=1)
    )


def history(source, per_day=6, days=7, today=0):
    n = 0
    for day in range(1, days + 1):
        for _ in range(per_day):
            n += 1
            backdate(save(source, n)[1], day)
    for _ in range(today):
        n += 1
        save(source, n)
    CollectionRun.objects.create(source=source, status="ok", finished_at=NOW)


class TestVolume:
    def test_no_documents_incident_dedup_and_recovery(self, source, settings):
        history(source, today=0)
        monitoring.monitor_source(source, timezone.now())
        incident = SourceIncident.objects.get(source=source, incident_type="no_documents")
        assert incident.level == "critical" and incident.ended_at is None
        assert monitoring.health(source) == "red"
        check = SourceHealthCheck.objects.get(source=source, check_type="volume")
        assert check.status == "critical" and check.metrics["median"] == 6
        assert Notification.objects.filter(level="critical").count() == 1

        monitoring.monitor_source(source, timezone.now())  # повторный сигнал не спамит
        incident.refresh_from_db()
        assert incident.occurrences == 2 and Notification.objects.count() == 1

        for n in range(100, 106):
            save(source, n)
        monitoring.monitor_source(source, timezone.now())
        incident.refresh_from_db()
        assert incident.ended_at is not None
        assert Notification.objects.filter(title__contains="opět v normě").count() == 1
        assert monitoring.health(source) == "green"

    def test_low_volume(self, source):
        history(source, per_day=9, today=2)
        [result] = [
            r for r in monitoring.monitor_source(source, timezone.now()) if r.check_type == "volume"
        ]
        assert [s.incident_type for s in result.signals] == ["low_volume"]

    def test_no_norm_yet(self, source):
        history(source, days=2, today=0)
        [result] = [
            r for r in monitoring.monitor_source(source, timezone.now()) if r.check_type == "volume"
        ]
        assert not result.signals and "Norma ještě není k dispozici" in result.details

    def test_weekly_rhythm_uses_same_weekday_norm(self, source):
        """Тихий день недели (выходные у медиа) — не поломка, если он тихий всегда."""
        n = 0
        for day in range(1, monitoring.WEEKLY_HISTORY_DAYS + 1):
            for _ in range(1 if day % 7 == 0 else 6):
                n += 1
                backdate(save(source, n)[1], day)
        save(source, n + 1)  # сегодня, как и в тот же день прошлых недель, — один документ
        result = monitoring.check_volume(source, timezone.now())
        assert result.metrics["norm"] == "stejný den v týdnu" and result.metrics["median"] == 1
        assert not result.signals

    def test_flat_history_keeps_daily_norm(self, source):
        n = 0
        for day in range(1, monitoring.WEEKLY_HISTORY_DAYS + 1):
            for _ in range(6):
                n += 1
                backdate(save(source, n)[1], day)
        save(source, n + 1)
        result = monitoring.check_volume(source, timezone.now())
        assert result.metrics["norm"] == "den"
        assert [s.incident_type for s in result.signals] == ["low_volume"]

    def test_stale_feed(self, source):
        source.stale_after_hours = 12
        source.save()
        backdate(save(source, 1)[1], 1)
        CollectionRun.objects.create(source=source, status="ok")
        result = monitoring.check_volume(source, NOW)
        assert [s.incident_type for s in result.signals] == ["stale_feed"]


class TestExtractionAndHttp:
    def test_extraction_drop_and_quality(self, source):
        for n in range(6):
            save(source, n, published_at=None, date_source="none", title="")
        results = {r.check_type: r for r in monitoring.monitor_source(source, timezone.now())}
        assert [s.incident_type for s in results["extraction"].signals] == ["extraction"]
        assert "bez data 100%" in results["extraction"].signals[0].description
        assert results["quality"].metrics["mean"] == 3.0
        assert [s.incident_type for s in results["quality"].signals] == ["quality_drop"]

    def test_blocked_pages(self, source):
        for n in range(3):
            save(source, n, text=f"Just a moment... Checking your browser {n}")
        result = monitoring.check_extraction(source, timezone.now())
        assert [s.incident_type for s in result.signals] == ["blocked"]
        assert result.signals[0].level == "critical" and len(result.signals[0].examples) == 3

    def test_http_errors_and_unavailable_channel(self, source):
        for _ in range(2):
            CollectionRun.objects.create(
                source=source, status="failed", http_requests=4, finished_at=NOW,
                details={"http": {"http_503": 4}, "timeouts": 0, "redirect_offsite": 2,
                         "errors": ["@kanal: kanál není dostupný nebo má vypnutý náhled"]},
            )  # fmt: skip
        result = monitoring.check_http(source, NOW)
        types = {s.incident_type: s for s in result.signals}
        assert set(types) == {"http_errors", "channel_unavailable"}
        assert types["http_errors"].level == "critical"
        assert "chyb HTTP 100%" in types["http_errors"].description

    def test_monitor_task_skips_sources_without_runs(self, source):
        from apps.ingestion.tasks import monitor_sources

        assert monitor_sources() == {}
        CollectionRun.objects.create(source=source, status="ok", finished_at=NOW)
        assert list(monitor_sources()) == [source.pk]


def test_golden_page(source):
    page = GoldenPage.objects.create(
        source=source, url="https://z.example/old", expected_title="Vláda schválila rozpočet",
        expected_text_prefix=CZECH[:200],
    )  # fmt: skip
    html = (
        "<html lang='cs'><head><title>Vláda schválila rozpočet</title></head><body><article>"
        f"<h1>Vláda schválila rozpočet</h1><p>{CZECH}</p></article></body></html>"
    )
    assert monitoring.compare_golden(page, html) == []
    broken = html.replace(CZECH, "Přijměte prosím cookies, abychom mohli zobrazit obsah. " * 5)
    assert monitoring.compare_golden(page, broken) == ["začátek textu"]


# --- Telegram и панель ----------------------------------------------------------------------


def test_telegram_send(settings):
    settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID = "123:abc", "42"
    sent = []

    def handler(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    n = Notification.objects.create(
        notification_type="collection", level="critical", title="ČT24: Žádné dokumenty",
        text="0 dokumentů", url="/admin/actors/source/1/change/",
    )  # fmt: skip
    assert telegram.send(n, httpx.MockTransport(handler))
    url, body = sent[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage" and body["chat_id"] == "42"
    assert (
        body["text"].startswith("🔴 ČT24: Žádné dokumenty")
        and "/admin/actors/source/1/" in body["text"]
    )
    n.refresh_from_db()
    assert n.sent_to_telegram
    assert telegram.send_pending(httpx.MockTransport(handler)) == 0  # уже отправлено


def test_telegram_failure_is_retried_later(settings):
    settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID = "123:abc", "42"
    n = Notification.objects.create(notification_type="collection", title="X")
    assert not telegram.send(n, httpx.MockTransport(lambda r: httpx.Response(502)))
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    assert telegram.send_pending(ok) == 1


def test_telegram_not_configured(settings):
    settings.TELEGRAM_BOT_TOKEN = ""
    n = Notification.objects.create(notification_type="collection", title="X")
    assert not telegram.send(n)


def test_panel_pages(source, researcher_client):
    SourceIncident.objects.create(
        source=source, incident_type="stale_feed", level="warning", started_at=NOW,
        description="Kanál mlčí",
    )  # fmt: skip
    Notification.objects.create(
        notification_type="collection", level="warning", title="Zprávy: ticho"
    )
    page = researcher_client.get(reverse("panel:sources"))
    assert page.status_code == 200 and "health-yellow" in page.content.decode()
    page = researcher_client.get(reverse("panel:notifications"))
    assert "Zprávy: ticho" in page.content.decode()
    researcher_client.post(reverse("panel:notifications-read"))
    assert not Notification.objects.filter(is_read=False).exists()


def test_source_detail_page_with_daily_series(source, researcher_client):
    from apps.panel.views import CHART_DAYS, daily_series

    save(source, 1)
    save(source, 2, rubric="Sport")
    Document.objects.filter(rubric="Sport").update(in_scope=False)
    series = daily_series(source)
    assert len(series["dates"]) == CHART_DAYS
    assert (series["volume"][-1], series["in_scope"][-1]) == (2, 1)
    assert series["scores"]["5"][-1] == 2 and series["mean_quality"][-1] == 5.0
    GoldenPage.objects.create(
        source=source, url="https://z.example/a/1", expected_title="Titulek 1",
        expected_text_prefix="Vláda",
    )  # fmt: skip
    page = researcher_client.get(reverse("panel:source_detail", args=[source.pk]))
    content = page.content.decode()
    assert page.status_code == 200 and "source-series" in content
    assert "Kontrolní stránky" in content and "https://z.example/a/1" in content


def test_propose_golden_pages_from_clean_documents(source):
    from django.core.management import call_command

    for n in range(4):
        _, doc = save(source, n, published_at=NOW - datetime.timedelta(days=n + 2))
        backdate(doc, n + 2)
    call_command("propose_golden_pages", stdout=open("/dev/null", "w"))  # noqa: SIM115
    pages = GoldenPage.objects.filter(source=source)
    assert pages.count() == 3
    assert all(p.expected_text_prefix.startswith("Vláda") for p in pages)


def test_panel_pages_closed_for_observer(observer_client):
    assert observer_client.get(reverse("panel:sources")).status_code == 403
    assert observer_client.get(reverse("panel:source_detail", args=[1])).status_code == 403
    assert observer_client.get(reverse("panel:notifications")).status_code == 403


def test_assess_quality_command(source):
    from django.core.management import call_command

    _, doc = save(source, 1)
    DocumentQuality.objects.all().delete()
    call_command("assess_quality", stdout=open("/dev/null", "w"))  # noqa: SIM115
    assert current(doc).score == 5
