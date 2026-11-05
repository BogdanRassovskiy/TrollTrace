"""Перепроверка после инцидентов: извлечение тела статьи, потерянные URL, зависшие запуски."""

import datetime

import httpx
import pytest
from django.utils import timezone

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.documents.models import Document
from apps.ingestion import tasks
from apps.ingestion.extract import _overlaps, body_text, extract_article
from apps.ingestion.http import PoliteClient
from apps.ingestion.models import CollectionRun, SourceFeed, SourceIncident
from apps.ingestion.store import Item, save_item
from apps.panel.models import Job, Notification

pytestmark = pytest.mark.django_db

SIDEBAR = (
    "Marija Zacharová prohlásila, že kanadská vláda nevydá víza ruským sportovcům pro účast "
    "na mistrovství světa, které se bude konat v Edmontonu koncem prosince letošního roku. "
) * 3


def page(title: str, body: str) -> str:
    """Как у сети Pravda: короткий пост в articleBody и длинный блок «Top novinky» рядом —
    trafilatura на такой странице берёт ленту анонсов вместо поста."""
    items = "".join(
        f"<div class='news-list__item'><a class='news-item__title' href='/x/{i}'>Titulek {i}</a>"
        f"<div class='news-item__desc'>{SIDEBAR}</div></div>"
        for i in range(5)
    )
    return (
        f"<html lang='cs'><body><div class='container'><article><h1>{title}</h1>"
        f"<div itemprop='articleBody'><p>{body}</p></div></article><div class='news-block'>"
        f"<div class='news-block__sub-title'>Top novinky</div><div class='news-list'>{items}"
        "</div></div></div></body></html>"
    )


@pytest.fixture
def source():
    source = Source.objects.create(
        name="Pravda", layer=Layer.ATTRIBUTED, platform=Platform.WEB, parser="example",
        url="https://p.example/", expected_language="cs", min_text_length=300,
    )  # fmt: skip
    actor = Actor.objects.create(name="Pravda", actor_type="media", layer=Layer.ATTRIBUTED)
    ActorAccount.objects.create(actor=actor, source=source, account_id="")
    return source


class TestArticleBody:
    def test_article_body_wins_over_sidebar(self):
        from trafilatura import bare_extraction

        html = page("Stávka", "Vstupujeme do stávky! https://youtube.com/watch?v=1")
        assert "Zacharová" in bare_extraction(html, favor_precision=True).text  # сама ошиблась бы
        text = extract_article(html, "https://p.example/1").text
        assert text == "Vstupujeme do stávky! https://youtube.com/watch?v=1"

    def test_trafilatura_kept_when_texts_overlap(self):
        """iDNES: articleBody начинается со служебной строки — остаётся текст trafilatura."""
        from trafilatura import bare_extraction

        text = "Vláda dnes schválila rozpočet na příští rok. " * 10
        html = (
            "<html lang='cs'><body><article><h1>Rozpočet</h1><div itemprop='articleBody'>"
            f"<p>Podívejte se i na naše další živé přenosy</p><p>{text}</p></div></article>"
            "</body></html>"
        )
        own = bare_extraction(html, url="https://p.example/2", with_metadata=True,
                              include_comments=False, favor_precision=True).text  # fmt: skip
        assert body_text(html).startswith("Podívejte") and _overlaps(own, body_text(html))
        assert extract_article(html, "https://p.example/2").text == own.strip()

    def test_source_xpath(self):
        html = "<html><body><div id='a'><p>Jedna.</p><p>Dva.</p></div><p>Jinde.</p></body></html>"
        extracted = extract_article(html, "https://p.example/3", text_xpath="//div[@id='a']")
        assert extracted.text == "Jedna.\nDva."


def test_reextract_and_recover_lost_urls(source):
    """Ошибка извлечения слила разные посты в «один текст»: после исправления текст
    переизвлекается из сохранённого HTML, а отброшенные URL собираются заново."""
    actor = source.accounts.get().actor
    first = Item(url="https://p.example/1", title="Stávka", text=SIDEBAR,
                 raw_html=page("Stávka", "Vstupujeme do stávky!"), language="cs")  # fmt: skip
    _, doc = save_item(source, actor, first)
    for n in (2, 3):
        save_item(source, actor, Item(url=f"https://p.example/{n}", title="Jiné", text=SIDEBAR))
    doc.refresh_from_db()
    assert doc.meta["duplicate_urls"] == ["https://p.example/2", "https://p.example/3"]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        n = request.url.path.strip("/")
        return httpx.Response(200, text=page(f"Post {n}", f"Úplně jiný post číslo {n}."))

    incident = SourceIncident.objects.create(
        source=source, incident_type="boilerplate", level="critical",
        started_at=timezone.now(), ended_at=timezone.now(),
    )  # fmt: skip
    client = PoliteClient(delay=0, transport=httpx.MockTransport(handler))
    counts = tasks.recheck_incident_documents(incident, client)
    assert counts["reextracted"] == 1 and counts["recovered"] == 2
    doc.refresh_from_db()
    assert doc.text == "Vstupujeme do stávky!" and "duplicate_urls" not in doc.meta
    assert doc.versions.count() == 2 and doc.processed_at is None
    assert set(Document.objects.values_list("text", flat=True)) == {
        "Vstupujeme do stávky!", "Úplně jiný post číslo 2.", "Úplně jiný post číslo 3."
    }  # fmt: skip


def test_recheck_incident_task_records_job(source, monkeypatch):
    incident = SourceIncident.objects.create(
        source=source, incident_type="boilerplate", level="critical",
        started_at=timezone.now(), ended_at=timezone.now(),
    )  # fmt: skip
    counts = {"documents": 3, "reextracted": 1, "recovered": 0, "refetched": 0}
    monkeypatch.setattr(
        tasks, "recheck_incident_documents", lambda inc: {**counts, "changed_score": 0}
    )
    tasks.recheck_incident(incident.pk)
    job = Job.objects.get()
    assert job.job_type == Job.Type.QUALITY and job.status == Job.Status.SUCCEEDED
    assert "znovu extrahováno 1" in job.message


def test_resolve_queues_recheck(source, monkeypatch, django_capture_on_commit_callbacks):
    from apps.ingestion import monitoring

    queued = []
    monkeypatch.setattr(tasks.recheck_incident, "delay", queued.append)
    incident = SourceIncident.objects.create(
        source=source, incident_type="boilerplate", level="critical", started_at=timezone.now()
    )
    with django_capture_on_commit_callbacks(execute=True):
        monitoring.resolve(source, "boilerplate", timezone.now())
    assert queued == [incident.pk]


def test_stale_runs_closed(source):
    stale = CollectionRun.objects.create(source=source)
    fresh = CollectionRun.objects.create(source=source)
    CollectionRun.objects.filter(pk=stale.pk).update(
        started_at=timezone.now() - datetime.timedelta(hours=3)
    )
    assert tasks.close_stale_runs() == 1
    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == CollectionRun.Status.FAILED and "přerušen" in stale.details["errors"][0]
    assert fresh.status == CollectionRun.Status.RUNNING


def test_robots_disallowed_feed_is_switched_off(source):
    from apps.ingestion.collectors.web import WebCollector

    feed = SourceFeed.objects.create(source=source, url="https://zakaz.example/rss", kind="rss")

    def handler(request):
        return httpx.Response(200, text="User-agent: *\nDisallow: /rss\n")

    client = PoliteClient(delay=0, transport=httpx.MockTransport(handler))
    WebCollector(source, client).collect()
    feed.refresh_from_db()
    assert not feed.is_active and "robots.txt" in feed.last_error
    assert Notification.objects.get().title.endswith("kanál vypnut kvůli robots.txt")
