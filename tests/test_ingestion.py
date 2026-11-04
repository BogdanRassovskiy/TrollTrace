"""Этап 3: сбор данных.

HTTP подменяется httpx.MockTransport, разметка фикстур — как у реальных источников.
"""

import datetime
import io
import zipfile

import httpx
import pytest
from django.core.management import call_command
from django.utils import timezone
from PIL import Image

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.documents.models import Document
from apps.documents.utils import normalize_url
from apps.ingestion import psp
from apps.ingestion.collectors.telegram import parse_channel_page, parse_count
from apps.ingestion.collectors.web import parse_sitemap
from apps.ingestion.deputies import ICON_SIZE, sync_deputies
from apps.ingestion.http import PoliteClient, RobotsDisallowed
from apps.ingestion.models import CollectionRun, SourceFeed
from apps.ingestion.store import Item, save_item
from apps.ingestion.tasks import (
    collect_source,
    documents_to_recheck,
    due_sources,
    recheck_documents,
    run_collector,
)
from apps.panel.models import AuditLog

pytestmark = pytest.mark.django_db


class FakeWeb:
    """Маршрутизатор для MockTransport: url → (статус, тело, заголовки); остальное — 404."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, dict]] = {}
        self.requests: list[str] = []

    def add(self, url, body, status=200, headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.routes[url] = (status, body, headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.routes:
            status, body, headers = self.routes[url]
            if request.headers.get("if-none-match") and request.headers[
                "if-none-match"
            ] == headers.get("etag"):
                return httpx.Response(304)
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(404, content=b"not found")

    def client(self):
        return PoliteClient(delay=0, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


def article_html(text="", published="2026-10-30T08:15:00+01:00", title="Vláda schválila rozpočet"):
    paragraphs = text or (
        "Vláda na svém dnešním jednání schválila návrh státního rozpočtu na příští rok. "
        "Schodek má podle ministra financí dosáhnout přibližně dvě stě miliard korun. "
        "Opozice návrh kritizuje a požaduje hlubší škrty ve výdajích státu. "
        "Rozpočet nyní míří do Poslanecké sněmovny, kde ho čeká první čtení. "
        "Ekonomové upozorňují na rostoucí náklady na obsluhu státního dluhu."
    )
    body = "".join(f"<p>{paragraphs} Odstavec {i}.</p>" for i in range(4))
    return f"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
    <title>{title} | Zprávy</title>
    <meta property="og:title" content="{title}">
    <meta property="article:published_time" content="{published}">
    </head><body><header><nav>Domů · Svět · Sport</nav></header>
    <article><h1>{title}</h1>{body}</article>
    <footer>© Zprávy 2026</footer></body></html>"""


def rss(*items):
    entries = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate>"
        f"<category>Domácí</category></item>"
        for u, t, d in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>Z</title>{entries}'
        "</channel></rss>"
    )


def web_source(**kwargs):
    source = Source.objects.create(
        name="Zprávy",
        layer=Layer.MAINSTREAM,
        platform=Platform.WEB,
        url="https://zpravy.example/",
        **kwargs,
    )
    actor = Actor.objects.create(name="Zprávy", actor_type=Actor.Type.MEDIA, layer=Layer.MAINSTREAM)
    ActorAccount.objects.create(actor=actor, source=source, account_id="")
    SourceFeed.objects.create(source=source, url="https://zpravy.example/rss")
    return source


# --- HTTP ---------------------------------------------------------------------------------


class TestPoliteClient:
    def test_robots_respected_and_cached(self, web):
        web.add("https://a.example/robots.txt", "User-agent: *\nDisallow: /private/\n")
        web.add("https://a.example/ok", "ok")
        client = web.client()
        assert client.get("https://a.example/ok").text == "ok"
        with pytest.raises(RobotsDisallowed):
            client.get("https://a.example/private/x")
        assert web.requests.count("https://a.example/robots.txt") == 1

    def test_longest_match_wins(self):
        from apps.ingestion.robots import Robots

        robots = Robots(
            "User-agent: *\nDisallow: /\nAllow: /rss.aspx\n\n"
            "User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n\n"
            "User-agent: Other\nDisallow: /*.pdf$\n"
        )
        ua = "TrollTraceResearchBot/0.1 (+https://example.org)"
        assert robots.can_fetch(ua, "https://s.example/rss.aspx?c=zpravodaj")
        assert not robots.can_fetch(ua, "https://s.example/clanek")
        assert not robots.can_fetch("ClaudeBot/1.0", "https://s.example/rss.aspx")
        assert not robots.can_fetch("Other", "https://s.example/a/b.pdf")
        assert robots.can_fetch("Other", "https://s.example/a/b.pdf?x=1")

    def test_server_error_on_robots_means_no_crawling(self, web):
        web.add("https://b.example/robots.txt", "boom", status=503)
        with pytest.raises(RobotsDisallowed):
            web.client().get("https://b.example/page")

    def test_user_agent_is_research_bot(self, web, settings):
        seen = {}

        def handler(request):
            seen["ua"] = request.headers["user-agent"]
            return httpx.Response(200, content=b"")

        PoliteClient(delay=0, transport=httpx.MockTransport(handler)).get("https://c.example/")
        assert seen["ua"] == settings.TROLLTRACE["CRAWLER_USER_AGENT"]


def test_normalize_url_keeps_fragment_on_request():
    url = "https://www.psp.cz/eknih/2025ps/stenprot/001schuz/s001002.htm#r1"
    assert normalize_url(url) == "https://psp.cz/eknih/2025ps/stenprot/001schuz/s001002.htm"
    assert normalize_url(url, keep_fragment=True).endswith("s001002.htm#r1")


# --- Сайты --------------------------------------------------------------------------------


class TestWebCollector:
    def setup_feed(self, web, etag=""):
        web.add(
            "https://zpravy.example/rss",
            rss(
                (
                    "https://zpravy.example/a/1?utm_source=rss",
                    "Rozpočet",
                    "Thu, 30 Oct 2026 07:15:00 GMT",
                ),
                ("https://zpravy.example/a/2", "Jiný článek", "Thu, 30 Oct 2026 09:00:00 GMT"),
            ),
            headers={"etag": etag} if etag else None,
        )
        web.add("https://zpravy.example/a/1?utm_source=rss", article_html())
        web.add(
            "https://zpravy.example/a/2",
            article_html(
                text="Úplně jiný text o zahraniční politice a sankcích. " * 8, title="Sankce"
            ),
        )

    def test_collects_articles_with_raw_html_and_two_times(self, web):
        source = web_source()
        self.setup_feed(web)
        run = run_collector(source, web.client())

        assert (run.status, run.found, run.new) == ("ok", 2, 2)
        doc = Document.objects.get(url_normalized="https://zpravy.example/a/1")
        assert doc.title == "Vláda schválila rozpočet"
        assert "státního rozpočtu" in doc.text
        assert doc.published_at == datetime.datetime(2026, 10, 30, 7, 15, tzinfo=datetime.UTC)
        assert doc.date_source == Document.DateSource.PAGE
        assert doc.collected_at > doc.published_at
        assert (doc.language, doc.rubric) == ("cs", "Domácí")
        version = doc.versions.get()
        assert "<article>" in version.raw_html and version.text_hash == doc.text_hash

    def test_second_run_skips_known_and_uses_conditional_get(self, web):
        source = web_source()
        self.setup_feed(web, etag='"v1"')
        run_collector(source, web.client())
        web.requests.clear()
        run = run_collector(source, web.client())
        assert run.new == 0
        assert not any("/a/" in url for url in web.requests)  # статьи заново не качаются
        feed = source.feeds.get()
        assert feed.etag == '"v1"' and feed.last_success_at is not None

    def test_same_text_under_other_url_is_duplicate(self, web):
        source = web_source()
        web.add(
            "https://zpravy.example/rss",
            rss(
                ("https://zpravy.example/a/1", "A", "Thu, 30 Oct 2026 07:15:00 GMT"),
                ("https://zpravy.example/amp/a/1", "A", "Thu, 30 Oct 2026 07:16:00 GMT"),
            ),
        )
        web.add("https://zpravy.example/a/1", article_html())
        web.add("https://zpravy.example/amp/a/1", article_html())
        run = run_collector(source, web.client())
        assert (run.new, run.duplicates) == (1, 1)

    def test_feed_errors_recorded_not_raised(self, web):
        source = web_source()  # лента отвечает 404
        run = run_collector(source, web.client())
        assert run.status == CollectionRun.Status.FAILED
        assert "HTTP 404" in run.details["errors"][0]
        assert source.feeds.get().last_error == "HTTP 404"

    def test_excluded_actor_is_not_collected(self, web):
        source = web_source()
        self.setup_feed(web)
        Actor.objects.update(status=Actor.Status.EXCLUDED)
        run = run_collector(source, web.client())
        assert run.new == 0 and run.errors == 1

    def test_recheck_records_edit_and_deletion(self, web):
        source = web_source()
        self.setup_feed(web)
        run_collector(source, web.client())
        Document.objects.update(collected_at=timezone.now() - datetime.timedelta(days=2))
        assert documents_to_recheck().count() == 2

        web.add(
            "https://zpravy.example/a/1?utm_source=rss",
            article_html(text="Opravený text článku. " * 30),
        )
        web.routes.pop("https://zpravy.example/a/2")  # → 404
        from apps.ingestion import tasks

        tasks.PoliteClient = lambda **kw: web.client()  # noqa: E731 — подмена на время теста
        try:
            counts = recheck_documents()
        finally:
            tasks.PoliteClient = PoliteClient
        assert counts == {"version": 1, "deleted": 1}
        edited = Document.objects.get(url_normalized="https://zpravy.example/a/1")
        assert edited.versions.count() == 2 and "Opravený" in edited.text
        deleted = Document.objects.get(url_normalized="https://zpravy.example/a/2")
        assert deleted.status == Document.Status.DELETED and deleted.deleted_at
        assert documents_to_recheck().count() == 0  # до 7-дневной перепроверки


def test_parse_sitemap_index_and_news():
    index = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://p.example/s-old.xml</loc><lastmod>2026-10-01</lastmod></sitemap>
      <sitemap><loc>https://p.example/s-new.xml</loc><lastmod>2026-10-30</lastmod></sitemap>
    </sitemapindex>"""
    assert parse_sitemap(index) == (
        [],
        ["https://p.example/s-new.xml", "https://p.example/s-old.xml"],
    )
    news = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
      xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url><loc>https://p.example/x</loc><news:news><news:publication_date>2026-10-30T10:00:00+01:00
      </news:publication_date><news:title>Titulek</news:title></news:news></url></urlset>"""
    entries, children = parse_sitemap(news)
    assert (
        children == [] and entries[0].url == "https://p.example/x" and entries[0].title == "Titulek"
    )


# --- Telegram -----------------------------------------------------------------------------


def tg_message(channel, msg_id, text, when, views="1.2K", forwarded=None):
    fwd = (
        f'<div class="tgme_widget_message_forwarded_from">Forwarded from '
        f'<a class="tgme_widget_message_forwarded_from_name" href="https://t.me/{forwarded}">{forwarded}</a></div>'
        if forwarded
        else ""
    )
    return f"""<div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message js-widget_message" data-post="{channel}/{msg_id}">
        <div class="tgme_widget_message_bubble">{fwd}
          <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
          <div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">
            <span class="tgme_widget_message_views">{views}</span>
            <span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/{channel}/{msg_id}">
            <time datetime="{when}" class="time">10:00</time></a></span>
          </div></div>
        </div></div></div>"""


def tg_page(channel, *messages):
    history = "".join(messages)
    return f"<html><body><section class='tgme_channel_history'>{history}</section></body></html>"


def telegram_source(handle="kanal_cz"):
    source = Source.objects.create(
        name="Telegram", layer=Layer.TELEGRAM, platform=Platform.TELEGRAM, url="https://t.me/",
        collector_config={"backfill_pages": 2},
    )  # fmt: skip
    actor = Actor.objects.create(name="Kanál", actor_type=Actor.Type.CHANNEL, layer=Layer.TELEGRAM)
    ActorAccount.objects.create(actor=actor, source=source, account_id=handle)
    return source


class TestTelegram:
    def test_parse_message(self):
        page = tg_page(
            "kanal_cz",
            tg_message(
                "kanal_cz",
                7,
                "První řádek<br/>Druhý řádek",
                "2026-10-30T09:00:00+00:00",
                "3.4K",
                "zdroj",
            ),
        )
        [msg] = parse_channel_page(page)
        assert msg["id"] == 7 and msg["text"] == "První řádek\nDruhý řádek"
        assert msg["views"] == 3400 and msg["forwarded_from"]["name"] == "zdroj"
        assert msg["published_at"] == datetime.datetime(2026, 10, 30, 9, tzinfo=datetime.UTC)

    @pytest.mark.parametrize(
        ("raw", "value"), [("950", 950), ("1.2K", 1200), ("3M", 3_000_000), ("", None)]
    )
    def test_parse_count(self, raw, value):
        assert parse_count(raw) == value

    def test_backfill_then_incremental(self, web):
        source = telegram_source()
        t = "2026-10-30T09:00:00+00:00"
        web.add(
            "https://t.me/s/kanal_cz",
            tg_page("kanal_cz", *(tg_message("kanal_cz", i, f"Zpráva {i}", t) for i in (11, 12))),
        )
        web.add(
            "https://t.me/s/kanal_cz?before=11",
            tg_page("kanal_cz", *(tg_message("kanal_cz", i, f"Zpráva {i}", t) for i in (9, 10))),
        )
        run = run_collector(source, web.client())
        assert run.new == 4
        doc = Document.objects.get(external_id="kanal_cz/12")
        assert doc.url == "https://t.me/kanal_cz/12" and doc.engagement == {"views": 1200}

        # Новый пост: читается одна страница, просмотры старых обновляются.
        web.add(
            "https://t.me/s/kanal_cz",
            tg_page(
                "kanal_cz",
                *(tg_message("kanal_cz", i, f"Zpráva {i}", t, views="2K") for i in (12, 13)),
            ),
        )
        web.requests.clear()
        run = run_collector(source, web.client())
        assert (run.new, run.unchanged) == (1, 1)
        assert "https://t.me/s/kanal_cz?before=12" not in web.requests
        assert Document.objects.get(external_id="kanal_cz/12").engagement == {"views": 2000}

    def test_unavailable_channel(self, web):
        source = telegram_source("zmizely")
        web.add(
            "https://t.me/s/zmizely", "", status=302, headers={"location": "https://t.me/zmizely"}
        )
        web.add("https://t.me/zmizely", "<html>channel</html>")
        run = run_collector(source, web.client())
        assert "není dostupný" in run.details["errors"][0]


# --- PSP ----------------------------------------------------------------------------------

STENO_PAGE = """<html><body><div id="main-content"><div class="document-nav">
<p class="date"><a href="1-1.html">Pondělí&nbsp;3.&nbsp;listopadu&nbsp;2025</a></p></div>
(14.10 hodin)<br>(pokračuje Věra Kovářová)<br><br>
<p>Nyní přistoupíme k&nbsp;druhému bodu.</p>
<p><b><a href="/sqw/detail.sqw?id=6621" id="r1">Poslanec Karel Havlíček</a></b>:
Slibuji věrnost České republice.</p>
<p>Slibuji na svou čest.</p>
<p><b><a href="/sqw/detail.sqw?id=9999" id="r2">Ministr vnitra Jan Neznámý</a></b>:
Děkuji za slovo.</p>
<p><b><a href="/sqw/detail.sqw?id=5000" id="r3">Poslankyně Eva Bývalá</a></b>:
Už nejsem poslankyní.</p>
</div></body></html>"""


def test_parse_steno_page():
    page = psp.parse_steno_page(STENO_PAGE)
    assert page.date == datetime.date(2025, 11, 3) and page.time == datetime.time(14, 10)
    assert [(s.anchor, s.speaker_id) for s in page.speeches] == [
        ("pokracovani", None), ("r1", 6621), ("r2", 9999), ("r3", 5000),
    ]  # fmt: skip
    assert page.speeches[0].speaker_label == "Věra Kovářová"
    assert page.speeches[1].text == "Slibuji věrnost České republice.\nSlibuji na svou čest."


def test_psp_collector(web):
    source = Source.objects.create(
        name="PSP", layer=Layer.PARLIAMENT, platform=Platform.PARLIAMENT, url=psp.SOURCE_URL,
        collector_config={"term": "2025ps"}, min_text_length=10,
    )  # fmt: skip
    for psp_id, name, status in [
        (6621, "Karel Havlíček", "active"),
        (1, "Věra Kovářová", "active"),
        (5000, "Eva Bývalá", "excluded"),
    ]:
        actor = Actor.objects.create(name=name, actor_type="politician", layer="E", status=status)
        ActorAccount.objects.create(actor=actor, source=source, account_id=str(psp_id))

    base = "https://www.psp.cz/eknih/2025ps/stenprot/"
    web.add(base + "index.htm", '<a href="001schuz/1-1.html">1</a>')
    web.add(
        base + "001schuz/1-1.html",
        '<a href="s001002.htm#r1">Havlíček</a><a href="s001002.htm#r2">x</a>',
    )
    web.add(
        base + "001schuz/s001002.htm",
        STENO_PAGE.encode("cp1250"),
        headers={"content-type": "text/html; charset=windows-1250"},
    )
    run = run_collector(source, web.client())
    assert run.new == 3 and run.skipped == 1  # бывшая депутатка пропущена

    speech = Document.objects.get(external_id="2025ps/001schuz/s001002.htm#r1")
    assert speech.actor.name == "Karel Havlíček"
    assert speech.url.endswith("s001002.htm#r1") and speech.url_normalized.endswith("#r1")
    assert speech.published_at == datetime.datetime(2025, 11, 3, 13, 10, tzinfo=datetime.UTC)
    assert speech.meta["meeting"] == 1 and "Slibuji" in speech.text
    assert Document.objects.get(external_id__endswith="#pokracovani").actor.name == "Věra Kovářová"

    newcomer = Document.objects.get(external_id__endswith="#r2").actor
    assert newcomer.name == "Jan Neznámý" and newcomer.role == "Ministr vnitra"
    assert "zkontrolovat" in newcomer.inclusion_criterion

    web.requests.clear()
    assert run_collector(source, web.client()).new == 0
    assert base + "001schuz/s001002.htm" not in web.requests  # страница уже обработана


# --- Депутаты -----------------------------------------------------------------------------


def registry_zip(end_kott="", club_change=False):
    def unl(rows):
        return "\n".join("|".join(r) + "|" for r in rows).encode("cp1250")

    files = {
        "organy": [
            [
                "173",
                "",
                "11",
                "PSP9",
                "Poslanecká sněmovna",
                "",
                "09.10.2021",
                "08.10.2025",
                "",
                "0",
            ],
            ["174", "", "11", "PSP10", "Poslanecká sněmovna", "", "04.10.2025", "", "174", ""],
            ["1750", "174", "1", "ANO2011", "Klub ANO", "", "08.10.2025", "", "1", ""],
            ["1751", "174", "1", "ODS", "Klub ODS", "", "08.10.2025", "", "2", ""],
            ["1760", "174", "3", "VB", "Výbor pro bezpečnost", "", "11.11.2025", "", "3", ""],
        ],
        "funkce": [
            ["900", "174", "11", "Předseda", "1", ""],
            ["901", "174", "12", "Ověřovatel", "5", ""],
            ["902", "1760", "13", "Předseda", "1", ""],
        ],
        "osoby": [
            ["6621", "doc. Ing.", "Havlíček", "Karel", "Ph.D.", "1969", "M", "", ""],
            ["6246", "", "Kott", "Josef", "", "1961", "M", "", ""],
            ["7000", "Mgr.", "Nová", "Jana", "", "1990", "Z", "", ""],
        ],
        "poslanec": [
            ["2045", "6621", "581", "1104", "174"],
            ["2046", "6246", "581", "1104", "174"],
            ["2047", "7000", "581", "1104", "174"],
            ["1804", "6621", "582", "1104", "173"],
        ],
        "zarazeni": [
            ["6621", "174", "0", "2025-10-04 00", "", "", ""],
            ["6246", "174", "0", "2025-10-04 00", end_kott, "", ""],
            ["7000", "174", "0", "2025-10-04 00", "", "", ""],
            ["6621", "1750", "0", "2025-10-08 00", "", "", ""],
            ["6246", "1750", "0", "2025-10-08 00", "", "", ""],
            ["7000", "1751", "0", "2025-10-08 00", "2026-03-01 00" if club_change else "", "", ""],
            *([["7000", "1750", "0", "2026-03-01 00", "", "", ""]] if club_change else []),
            ["7000", "900", "1", "2025-11-05 00", "", "", ""],  # předsedkyně Sněmovny
            ["6246", "901", "1", "2025-11-05 00", "", "", ""],  # ověřovatel — ne vedení
            ["6621", "1760", "0", "2025-11-11 00", "", "", ""],  # člen výboru
            ["6621", "902", "1", "2025-11-12 00", "", "", ""],  # předseda výboru
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, rows in files.items():
            archive.writestr(f"{name}.unl", unl(rows))
    return buffer.getvalue()


def portrait_jpeg():
    out = io.BytesIO()
    Image.new("RGB", (300, 400), (180, 120, 90)).save(out, "JPEG")
    return out.getvalue()


def test_sync_deputies(web):
    web.add(psp.OPEN_DATA_URL, registry_zip())
    for id_osoba in (6246, 7000):
        web.add(psp.photo_url(datetime.date(2025, 10, 4), id_osoba), portrait_jpeg())
    # У Havlíčka фото только в каталоге прошлого созыва, где он тоже был депутатом.
    web.add(psp.photo_url(datetime.date(2021, 10, 9), 6621), portrait_jpeg())

    result = sync_deputies(web.client())
    assert (result.created, result.photos, result.photo_errors) == (3, 3, [])

    havlicek = ActorAccount.objects.get(account_id="6621").actor
    assert (havlicek.name, havlicek.party, havlicek.role) == (
        "Karel Havlíček",
        "ANO",
        "poslanec PSP",
    )
    assert havlicek.positions.get(kind="deputy").start_date == datetime.date(2025, 10, 4)
    assert havlicek.layer == "E" and havlicek.status == Actor.Status.ACTIVE
    assert havlicek.inclusion_periods.get().start_date == datetime.date(2025, 10, 4)
    assert "/2021ps/" in havlicek.photo_source_url
    with Image.open(havlicek.photo.path) as icon:
        assert icon.size == (ICON_SIZE, ICON_SIZE)
    assert ActorAccount.objects.get(account_id="7000").actor.role.startswith("poslankyně PSP")
    source = Source.objects.get(url=psp.SOURCE_URL)
    assert source.collector_config["term"] == "2025ps"
    assert AuditLog.objects.filter(action="create", content_type__model="actor").count() == 3

    # Повторный импорт: Kott сложил мандат, Nová перешла в другой клуб; фото не перекачиваются.
    web.add(psp.OPEN_DATA_URL, registry_zip(end_kott="2026-06-18 00", club_change=True))
    web.requests.clear()
    result = sync_deputies(web.client())
    assert (result.created, result.updated, result.photos) == (0, 1, 0)
    kott = ActorAccount.objects.get(account_id="6246").actor
    # Bývalý poslanec zůstává politikem: funkce končí, z výběru nevypadává.
    assert kott.status == Actor.Status.ACTIVE
    assert kott.inclusion_periods.get().end_date is None
    assert kott.positions.get(kind="deputy").end_date == datetime.date(2026, 6, 18)
    assert (kott.role, kott.office_status) == ("dříve poslanec PSP; bez veřejné funkce", "none")
    assert ActorAccount.objects.get(account_id="7000").actor.party == "ANO"
    assert not any(url.endswith(".jpg") for url in web.requests)


def test_chamber_functions_are_informative_except_chamber_leadership(web):
    web.add(psp.OPEN_DATA_URL, registry_zip())
    sync_deputies(web.client(), photos=False)

    nova = ActorAccount.objects.get(account_id="7000").actor
    assert nova.role == "poslankyně PSP; předsedkyně Poslanecké sněmovny"
    assert nova.positions.get(kind="chamber_leadership").title == (
        "předsedkyně Poslanecké sněmovny"
    )
    havlicek = ActorAccount.objects.get(account_id="6621").actor
    assert set(havlicek.positions.filter(kind="chamber").values_list("title", flat=True)) == {
        "člen (Výbor pro bezpečnost)",
        "předseda (Výbor pro bezpečnost)",
    }
    assert havlicek.role == "poslanec PSP"  # výbory jen informativně, ne v souhrnu
    kott = ActorAccount.objects.get(account_id="6246").actor
    assert kott.positions.get(kind="chamber").title == "ověřovatel (Poslanecká sněmovna)"

    sync_deputies(web.client(), photos=False)  # idempotentně
    assert havlicek.positions.count() == 3


def test_deputy_role_recognizes_both_female_codes():
    def role(gender):
        return psp.Deputy(1, "A", "B", "", "", gender, "ANO").role

    assert (role("M"), role("Z"), role("Ž")) == ("Poslanec PSP", "Poslankyně PSP", "Poslankyně PSP")


def test_deputy_who_is_also_minister_stays_in_sample_after_mandate(web):
    from apps.actors.models import ActorPosition

    web.add(psp.OPEN_DATA_URL, registry_zip())
    sync_deputies(web.client(), photos=False)
    kott = ActorAccount.objects.get(account_id="6246").actor
    ActorPosition.objects.create(actor=kott, kind="government", title="ministr obrany")

    web.add(psp.OPEN_DATA_URL, registry_zip(end_kott="2026-06-18 00"))
    sync_deputies(web.client(), photos=False)
    kott.refresh_from_db()
    assert kott.status == Actor.Status.ACTIVE  # zůstává ministrem
    assert kott.inclusion_periods.get().end_date is None
    assert kott.positions.get(kind="deputy").end_date == datetime.date(2026, 6, 18)
    assert kott.role == "ministr obrany"


# --- Расписание и команды -----------------------------------------------------------------


def test_due_sources_respect_interval():
    fresh = web_source(collect_interval_minutes=60)
    CollectionRun.objects.create(source=fresh)
    stale = Source.objects.create(name="B", layer="B", platform="web", url="https://b.example/")
    run = CollectionRun.objects.create(source=stale)
    CollectionRun.objects.filter(pk=run.pk).update(
        started_at=timezone.now() - datetime.timedelta(hours=2)
    )
    Source.objects.create(
        name="Off", layer="B", platform="web", url="https://c.example/", is_active=False
    )
    assert [s.name for s in due_sources()] == ["B"]


def test_collect_source_lock(monkeypatch):
    from django.core.cache import cache

    source = web_source()
    cache.add(f"ingestion:collect:{source.pk}", "1")
    assert collect_source(source.pk)["skipped"]
    assert not CollectionRun.objects.exists()


def test_import_sources_mainstream_is_idempotent():
    path = "apps/ingestion/sources/mainstream.json"
    call_command("import_sources", path, stdout=io.StringIO())
    call_command("import_sources", path, stdout=io.StringIO())
    assert Source.objects.filter(layer="D").count() == 7
    assert Actor.objects.filter(layer="D", actor_type="media").count() == 7
    assert SourceFeed.objects.count() == 15  # 10 RSS + 5 news sitemap (záloha, když RSS vynechá)
    assert SourceFeed.objects.filter(kind="sitemap").count() == 5
    assert Source.objects.get(name="Novinky.cz").collector_config["rubrics_in_scope"]
    irozhlas = Source.objects.get(name="iROZHLAS")
    rss = irozhlas.feeds.filter(kind="rss")
    assert set(rss.values_list("rubric", flat=True)) == {"Domov", "Svět"}
    assert irozhlas.accounts.get().actor.inclusion_periods.count() == 1


# --- Рубрики и рамки исследования (этап 3.2) ---------------------------------------------

BREADCRUMB_PAGE = """<html><head>
<script type="application/ld+json">{"@type": "BreadcrumbList", "itemListElement": [
 {"@type": "ListItem", "position": 1, "name": "Novinky.cz", "item": "https://www.novinky.cz/"},
 {"@type": "ListItem", "position": 2, "name": "Zahraniční", "item": "https://www.novinky.cz/sekce/zahranicni-2"},
 {"@type": "ListItem", "position": 3, "name": "Evropa", "item": "https://www.novinky.cz/sekce/zahranicni-evropa-5"},
 {"@type": "ListItem", "position": 4, "name": "Fico ztratil většinu", "item": "https://www.novinky.cz/clanek/zahranicni-evropa-fico-1"}
]}</script></head><body></body></html>"""  # noqa: E501


def test_page_rubric_from_breadcrumbs_without_article_crumb():
    from apps.ingestion.extract import page_rubric

    url = "https://www.novinky.cz/clanek/zahranicni-evropa-fico-1"
    assert page_rubric(BREADCRUMB_PAGE, "Fico ztratil většinu", url) == "Zahraniční / Evropa"
    blesk = '<meta property="mrf:sections" content="Celebrity;StarDance">'
    assert page_rubric(f"<html><head>{blesk}</head></html>") == "Celebrity / StarDance"
    assert page_rubric("<html><body>bez rubriky</body></html>") == ""


@pytest.mark.django_db
def test_rubric_scope_include_exclude_and_unknown():
    from apps.ingestion.scope import rubric_in_scope

    source = Source(
        collector_config={
            "rubrics_in_scope": ["Domácí", "Byznys"],
            "rubrics_out_of_scope": ["Byznys / Investice"],
        }
    )
    assert rubric_in_scope(source, "Domácí / Politika") is True
    assert rubric_in_scope(source, "domaci") is True  # без регистра и диакритики
    assert rubric_in_scope(source, "Byznys / Investice / Akcie") is False
    assert rubric_in_scope(source, "Sport / Hokej") is False
    assert rubric_in_scope(source, "") is None
    assert rubric_in_scope(Source(collector_config={}), "Sport") is True


@pytest.mark.django_db
def test_update_scope_reextracts_rubric_and_requeues():
    source = Source.objects.create(
        name="N", layer="D", platform="web", url="https://n.example/",
        collector_config={"rubrics_in_scope": ["Zahraniční"]},
    )  # fmt: skip
    actor = Actor.objects.create(name="N", actor_type="media", layer="D")
    ActorAccount.objects.create(actor=actor, source=source, account_id="")
    url = "https://www.novinky.cz/clanek/zahranicni-evropa-fico-1"
    _, doc = save_item(
        source, actor, Item(url=url, title="Fico ztratil většinu", text="x" * 400,
                            raw_html=BREADCRUMB_PAGE),
    )  # fmt: skip
    assert doc.rubric == "" and doc.in_scope is None
    Document.objects.filter(pk=doc.pk).update(processed_at=timezone.now())
    call_command("update_scope", "--reextract", stdout=io.StringIO())
    doc.refresh_from_db()
    assert (doc.rubric, doc.in_scope, doc.processed_at) == ("Zahraniční / Evropa", True, None)


# --- Слой A: атрибутированные сети (этап 3.2) ---------------------------------------------

MICRODATA_PAGE = """<html lang="en"><body>
<ul itemscope itemtype="http://schema.org/BreadcrumbList">
 <li itemprop="itemListElement" itemscope itemtype="http://schema.org/ListItem">
  <meta itemprop="name" content="Síť"><meta itemprop="position" content="1"></li>
 <li itemprop="itemListElement" itemscope itemtype="http://schema.org/ListItem">
  <a itemprop="item" href="https://sit.example/russia"><i itemprop="name">Rusko</i></a>
  <meta itemprop="position" content="2"></li>
</ul>
<div itemprop="articleSection">Ukrajina</div></body></html>"""


def test_page_rubric_from_microdata_and_sections():
    from apps.ingestion.extract import page_rubric

    assert page_rubric(MICRODATA_PAGE) == "Rusko"
    generic = MICRODATA_PAGE.replace("Rusko", "Novinka")
    assert page_rubric(generic) == "Ukrajina"  # общая крошка → разделы статьи


def test_parse_listing_same_host_pattern_and_scheme():
    from apps.ingestion.collectors.web import parse_listing

    html = b"""<a href="/entry/a">A</a><a href="http://sit.example/entry/b#x">B</a>
    <a href="/tag/x">tag</a><a href="https://jiny.example/entry/c">C</a><a href="/entry/a">A</a>"""
    entries = parse_listing(html, "https://sit.example/", r"^/entry/[^/]+$")
    assert [e.url for e in entries] == [
        "https://sit.example/entry/a",
        "https://sit.example/entry/b",
    ]


def test_listing_feed_collects_with_origin_and_text_language(web):
    source = web_source(
        collector_config={
            "listing_link_pattern": r"^/a/\d+$",
            "origin_xpath": '//*[@class="source-link"]/@data-source-url',
        }
    )
    source.feeds.update(url="https://zpravy.example/", kind=SourceFeed.Kind.LISTING)
    web.add("https://zpravy.example/", '<a href="/a/1">1</a><a href="/o-nas">O nás</a>')
    page = (
        article_html()
        .replace('lang="cs"', 'lang="en"')
        .replace(
            "</article>",
            '<span class="source-link" data-source-url="https://t.me/kanal"></span></article>',
        )
    )
    web.add("https://zpravy.example/a/1", page)
    run = run_collector(source, web.client())

    assert (run.status, run.found, run.new) == ("ok", 1, 1)
    doc = Document.objects.get()
    assert doc.meta["origin_urls"] == ["https://t.me/kanal"]
    assert doc.language == "cs"  # шаблонный lang="en" не перебивает язык текста


def test_disallowed_child_sitemap_does_not_drop_index(web):
    source = web_source()
    source.feeds.update(url="https://zpravy.example/index.xml", kind=SourceFeed.Kind.SITEMAP)
    web.add("https://zpravy.example/robots.txt", "User-agent: *\nDisallow: /*?*\n")
    web.add(
        "https://zpravy.example/index.xml",
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://zpravy.example/sm.xml?m=1</loc></sitemap></sitemapindex>",
    )
    run = run_collector(source, web.client())
    assert any("zakázáno v robots.txt" in e for e in run.details["errors"])
    assert source.feeds.get().last_error == "Kanál je prázdný"


def test_import_sources_attributed():
    call_command("import_sources", "apps/ingestion/sources/attributed.json", stdout=io.StringIO())
    sources = Source.objects.filter(layer="A")
    assert sources.count() == 2
    assert all(s.attribution_by and s.attribution_url for s in sources)
    assert Actor.objects.filter(layer="A", actor_type="attributed_network").count() == 2
    assert Source.objects.get(name="42TCen").feeds.filter(kind="listing").count() == 2
