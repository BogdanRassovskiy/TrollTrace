"""Этап 3.5: свой парсер у каждого веб-источника.

Фикстуры — настоящие страницы сайтов (без скриптов и стилей, gzip) в
tests/fixtures/parsers/<ключ>/<id>.html.gz и ожидаемые поля в <id>.json рядом.
Новый парсер без фикстур, как и веб-источник в sources/*.json без парсера, валит тесты.
"""

import datetime
import gzip
import io
import json
from pathlib import Path

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.documents.models import Document
from apps.ingestion import monitoring, parsers
from apps.ingestion.extract import PRAGUE
from apps.ingestion.models import SourceIncident
from apps.ingestion.parsers.base import SiteParser, inside
from apps.ingestion.store import Item, save_item
from apps.ingestion.tasks import recheck_document, reparse_document, run_collector
from apps.panel.forms import ActorCreateForm

from .test_ingestion import FakeWeb, article_html, rss, web_source

FIXTURES = Path(__file__).parent / "fixtures" / "parsers"
SITE_PARSERS = sorted(k for k in parsers.PARSERS if k != "example")  # example — тестовый
CASES = sorted(FIXTURES.glob("*/*.json"))

pytestmark = pytest.mark.django_db


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


# --- Фикстуры сайтов ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_parser_on_real_page(case):
    expected = json.loads(case.read_text(encoding="utf-8"))
    html = gzip.decompress(case.with_suffix("").with_suffix(".html.gz").read_bytes()).decode()
    parser = parsers.PARSERS[case.parent.name]
    assert parser.matches(expected["url"])

    result = parser.parse(html, expected["url"])
    paragraphs = result.text.split("\n")
    assert result.missing == []
    assert result.parser == parser.label
    assert result.title == expected["title"]
    assert result.published_at == datetime.datetime.fromisoformat(expected["published_at"])
    assert result.author == expected["author"]
    assert result.rubric == expected["rubric"]
    assert result.origin_urls == expected["origin_urls"]
    assert paragraphs[0] == expected["first_paragraph"]
    assert paragraphs[-1] == expected["last_paragraph"]
    assert (len(paragraphs), len(result.text)) == (expected["paragraphs"], expected["length"])


@pytest.mark.parametrize("key", SITE_PARSERS)
def test_every_parser_has_fixtures(key):
    assert len(list((FIXTURES / key).glob("*.json"))) >= 2, f"parser {key}: chybí fixtures"


def test_every_web_source_in_seeds_has_matching_parser():
    for path in sorted(Path("apps/ingestion/sources").glob("*.json")):
        for spec in json.loads(path.read_text(encoding="utf-8"))["sources"]:
            if spec.get("platform") != Platform.WEB:
                continue
            parser = parsers.PARSERS.get(spec.get("parser", ""))
            assert parser is not None, f"{path.name}: {spec['name']} nemá parser"
            assert parser.matches(spec["url"]), f"{spec['name']}: parser {parser.key} jiný web"


# --- Разбор -------------------------------------------------------------------------------


class Demo(SiteParser):
    key = "demo"
    hosts = ("demo.example",)
    perex = '//p[@class="lead"]'
    blocks = inside("//article")
    drop = '//aside | //article//p[@class="lead"]'
    date = '//span[@class="date"]'
    date_regex = (
        r"(?P<day>\d{1,2})\.\s*(?P<month>\w+)\s+(?P<year>\d{4})"
        r"(?:\s*•\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
    )


DEMO = (
    "<html><body><h1>Titulek</h1><span class='date'>4. listopadu 2026 • 17:15</span>"
    "<p class='lead'>Perex článku.</p><article><p class='lead'>Perex článku.</p>"
    "<p>První<br>řádek.</p><ul><li><p>Bod</p></li></ul><aside><p>Čtěte také</p></aside>"
    "</article></body></html>"
)


def test_parse_fields_perex_drop_br_and_nested_blocks():
    result = Demo().parse(DEMO, "https://demo.example/a")
    assert result.title == "Titulek"
    assert result.text == "Perex článku.\nPrvní řádek.\nBod"  # <br> — mezera, li>p jednou
    assert result.published_at == datetime.datetime(2026, 11, 4, 17, 15, tzinfo=PRAGUE)
    assert result.date_source == Document.DateSource.PAGE and result.missing == []


def test_day_only_date_takes_time_from_feed():
    html = DEMO.replace(" • 17:15", "")
    feed = datetime.datetime(2026, 11, 4, 9, 30, tzinfo=datetime.UTC)
    assert Demo().parse(html, "https://demo.example/a", feed).published_at == feed


def test_missing_selectors_fall_back_to_generic_extractor():
    html = article_html(title="Rozpočet").replace("<article>", "<main>").replace("</article>", "")
    result = Demo().parse(html, "https://demo.example/a")
    assert result.missing == ["text", "published_at"]
    assert "Vláda na svém dnešním jednání" in result.text  # text z obecného extraktoru
    assert parsers.document_meta(result)["selectors_missing"] == ["text", "published_at"]


def test_register_rejects_duplicate_key_and_parser_without_text_selector():
    with pytest.raises(ValueError):
        parsers.register(type("Dup", (SiteParser,), {"key": "ct24", "blocks": "//p"}))
    with pytest.raises(ValueError):
        parsers.register(type("NoText", (SiteParser,), {"key": "bez-textu"}))


def test_guess_by_host():
    assert parsers.guess("https://www.irozhlas.cz/zpravy-domov/x").key == "irozhlas"
    assert parsers.guess("https://volby.blesk.cz/clanek/1").key == "blesk"
    assert parsers.guess("https://neznamy-web.cz/") is None


# --- Правило: веб-источник без парсера не собирается ---------------------------------------


def test_active_web_source_requires_registered_parser():
    source = Source(name="Web", layer=Layer.ALTERNATIVE, platform=Platform.WEB,
                    url="https://web.example/")  # fmt: skip
    with pytest.raises(ValidationError) as error:
        source.clean()
    assert "vlastní parser" in str(error.value)
    source.parser = "neexistuje"
    assert "neexistuje" in source.parser_error()
    source.parser = "example"
    source.clean()
    assert not Source(platform=Platform.WEB, is_active=False).parser_error()
    assert not Source(platform=Platform.TELEGRAM).parser_error()


def test_import_sources_refuses_web_source_without_parser(tmp_path):
    path = tmp_path / "sources.json"
    spec = {"name": "Bez parseru", "url": "https://bez.example/", "layer": "B", "platform": "web"}
    path.write_text(json.dumps({"sources": [spec]}), encoding="utf-8")
    with pytest.raises(CommandError, match="vlastní parser"):
        call_command("import_sources", str(path), stdout=io.StringIO())
    assert not Source.objects.exists()


def test_collector_refuses_source_without_parser(web):
    source = web_source()
    Source.objects.filter(pk=source.pk).update(parser="")
    source.refresh_from_db()
    run = run_collector(source, web.client())
    assert run.status == "failed" and run.found == 0
    assert "vlastní parser" in run.details["errors"][0]
    incident = SourceIncident.objects.get(source=source)
    assert incident.incident_type == SourceIncident.Type.SELECTOR_MISSING
    assert incident.level == "critical"


def test_collected_document_carries_parser_meta_and_selectors_ok(web):
    source = web_source()
    web.add("https://zpravy.example/rss", rss(("https://zpravy.example/a/1", "A", "")))
    web.add("https://zpravy.example/a/1", article_html())
    run_collector(source, web.client())
    doc = Document.objects.get()
    assert doc.meta == {"parser": "example/1"}
    assert doc.quality_scores.get(is_current=True).selectors_ok is True


def test_missing_selectors_lower_quality_and_open_incident(web):
    source = web_source()
    links = [(f"https://zpravy.example/a/{i}", f"A{i}", "") for i in range(3)]
    web.add("https://zpravy.example/rss", rss(*links))
    for url, title, _ in links:  # вёрстка сменилась: тела статьи в <article> больше нет
        page = article_html(title=title).replace("<article>", "<main>").replace("</article>", "")
        web.add(url, page)
    run = run_collector(source, web.client())
    assert run.details["selectors_missing"] == 3
    quality = Document.objects.first().quality_scores.get(is_current=True)
    assert (quality.selectors_ok, quality.score) == (False, 2)
    incident = SourceIncident.objects.get(source=source)
    assert incident.incident_type == SourceIncident.Type.SELECTOR_MISSING
    assert "text 3×" in incident.description

    # Парсер починили и статьи снова находятся — инцидент закрывается.
    Document.objects.update(meta={"parser": "example/2"})
    result = monitoring.check_selectors(source, run.finished_at)
    monitoring.apply(source, result, run.finished_at)
    assert SourceIncident.objects.get(source=source).ended_at is not None


def test_save_item_replaces_parser_meta_of_existing_document():
    source = web_source()
    actor = Actor.objects.get()
    meta = {"parser": "example/1", "selectors_missing": ["title"], "final_url": "x"}
    item = Item(url="https://zpravy.example/a/1", text="Text " * 100, meta=meta)
    save_item(source, actor, item)
    save_item(source, actor, Item(url=item.url, text=item.text, meta={"parser": "example/2"}))
    assert Document.objects.get().meta == {"final_url": "x", "parser": "example/2"}


# --- Переизвлечение архива -----------------------------------------------------------------


def _archived(source, html, **fields):
    actor = ActorAccount.objects.get(source=source).actor
    doc = Document.objects.create(
        actor=actor, source=source, url="https://zpravy.example/a/1", title="Stará | Zprávy",
        text="Starý text z obecného extraktoru.", collected_at=datetime.datetime.now(datetime.UTC),
        **fields,
    )  # fmt: skip
    doc.record_version(text=doc.text, title=doc.title, raw_html=html)
    return doc


def test_reparse_document_dry_run_then_apply():
    source = web_source()
    published = datetime.datetime(2026, 10, 30, 8, 15, tzinfo=PRAGUE)
    doc = _archived(source, article_html(published=published.isoformat()), rubric="Domácí",
                    published_at=published + datetime.timedelta(seconds=20),
                    date_source=Document.DateSource.PAGE)  # fmt: skip

    changed, missing = reparse_document(doc, apply=False)
    assert set(changed) == {"text", "title", "language", "meta"} and missing == []
    doc.refresh_from_db()
    assert doc.title == "Stará | Zprávy" and doc.versions.count() == 1  # nic neuloženo

    reparse_document(doc)
    doc.refresh_from_db()
    assert doc.title == "Vláda schválila rozpočet" and doc.versions.count() == 2
    assert doc.text.startswith("Vláda na svém dnešním jednání")
    assert doc.meta == {"parser": "example/1"} and doc.processed_at is None
    assert doc.rubric == "Domácí"  # rubriku z lenty parser nepřepisuje
    assert reparse_document(doc, apply=False) == ([], [])


def test_reparse_takes_date_from_first_version_of_page():
    """ČTK pod stejnou adresou vydává aktualizovanou zprávu s novým datem."""
    source = web_source()
    first = article_html(published="2026-11-04T14:15:00+01:00")
    feed_time = datetime.datetime(2026, 11, 4, 13, 0, tzinfo=datetime.UTC)
    doc = _archived(source, first, date_source=Document.DateSource.FEED, published_at=feed_time)
    later = article_html(published="2026-11-05T14:13:00+01:00", text="Nová verze zprávy. " * 30)
    doc.record_version(text="Nová verze", title=doc.title, raw_html=later)

    changed, _ = reparse_document(doc)
    doc.refresh_from_db()
    assert "published_at" in changed and doc.date_source == Document.DateSource.PAGE
    assert doc.published_at == datetime.datetime(2026, 11, 4, 14, 15, tzinfo=PRAGUE)
    assert doc.text.startswith("Nová verze zprávy.")


def test_reparse_archive_command_reports_and_applies():
    source = web_source()
    _archived(source, article_html())
    out = io.StringIO()
    call_command("reparse_archive", stdout=out)
    assert "dokumentů 1, změněno 1" in out.getvalue() and "Nic neuloženo" in out.getvalue()
    assert Document.objects.get().versions.count() == 1
    call_command("reparse_archive", "--source", "example", "--apply", stdout=io.StringIO())
    assert Document.objects.get().versions.count() == 2


# --- Панель: новый веб без парсера ----------------------------------------------------------


def test_panel_new_web_source_without_parser_is_created_disabled():
    from .test_panel_actors import form_data

    data = form_data(source_mode="new", new_source_url="https://neznamy-web.cz/",
                     new_source_platform="web", new_source_layer="B")  # fmt: skip
    ok = lambda url: (True, "dostupný")  # noqa: E731
    form = ActorCreateForm(data, check_available=ok)
    assert not form.is_valid()
    assert any("není parser" in e for e in form.non_field_errors())
    form = ActorCreateForm({**data, "confirm": "on"}, check_available=ok)
    assert form.is_valid(), form.errors

    from apps.panel.actors import create_actor

    create_actor(None, form.draft(), queue=False)
    source = Source.objects.get(url="https://neznamy-web.cz/")
    assert (source.parser, source.is_active) == ("", False)


# --- Cookies zdroje a stránka souhlasu (ParlamentníListy) ---------------------------------


class ConsentWeb(FakeWeb):
    """Článek jen s cookie souhlasu, jinak přesměrování na stránku souhlasu."""

    def handler(self, request):
        url = str(request.url)
        if url == "https://zpravy.example/a/1" and "souhlas=ano" not in request.headers.get(
            "cookie", ""
        ):
            self.requests.append(url)
            return httpx.Response(302, headers={"location": "https://zpravy.example/souhlas?r=a"})
        return super().handler(request)


@pytest.fixture
def consent_web(monkeypatch):
    monkeypatch.setattr(parsers.PARSERS["example"], "gate", r"^/souhlas")
    web = ConsentWeb()
    web.add("https://zpravy.example/rss", rss(("https://zpravy.example/a/1", "A", "")))
    web.add("https://zpravy.example/a/1", article_html())
    web.add("https://zpravy.example/souhlas?r=a", "<html><body>Souhlasím a vstupuji</body></html>")
    return web


def test_consent_page_is_not_saved_and_reported(consent_web):
    run = run_collector(web_source(), consent_web.client())
    assert not Document.objects.exists()
    assert "obnovte cookies zdroje" in run.details["errors"][0]


def test_source_cookies_are_sent_only_to_its_host(consent_web):
    source = web_source(collector_config={"cookies": {"souhlas": "ano"}})
    client = consent_web.client()
    run_collector(source, client)
    assert Document.objects.get().title == "Vláda schválila rozpočet"
    assert client._client.cookies.get("souhlas", domain="zpravy.example") == "ano"
    assert client._client.cookies.get("souhlas", domain="jiny.example") is None


def test_recheck_skips_consent_page(consent_web):
    source = web_source(collector_config={"cookies": {"souhlas": "ano"}})
    run_collector(source, consent_web.client())
    doc = Document.objects.get()
    source.collector_config = {}  # cookie vypršela
    source.save()
    doc.refresh_from_db()
    assert recheck_document(doc, consent_web.client().use_source(source)) == "gate"
    assert doc.versions.count() == 1
