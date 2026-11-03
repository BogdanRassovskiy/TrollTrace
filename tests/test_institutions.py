"""Этап 3.3: слой I — институты безопасности, ручное кодирование событий, ленты органов."""

import datetime
import io
import json
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.actors.models import ANALYSIS_LAYERS, Actor, Layer, Source
from apps.ingestion.collectors.web import page_date, parse_listing
from apps.ingestion.extract import PRAGUE
from apps.institutions.models import Institution, InstitutionalEvent
from apps.narratives.models import Narrative, Topic
from apps.panel.defaults import get_setting

pytestmark = pytest.mark.django_db


@pytest.fixture
def institution():
    return Institution.objects.create(name="Bezpečnostní informační služba", abbreviation="BIS",
                                      group=Institution.Group.INTELLIGENCE)  # fmt: skip


@pytest.fixture
def narrative():
    topic = Topic.objects.create(code="ua", name="Ukrajina")
    return Narrative.objects.create(
        code="ua-aid-pointless", topic=topic, name="Pomoc Ukrajině nemá smysl", definition="…"
    )


def event(institution, **kwargs):
    values = {
        "institution": institution, "channel": "report", "date": datetime.date(2026, 6, 1),
        "event_type": "annual_report", "title": "Výroční zpráva 2025",
        "source_url": "https://www.bis.cz/vyrocni-zpravy/", "specificity": "general",
    }  # fmt: skip
    return InstitutionalEvent(**{**values, **kwargs})


def test_layer_i_is_not_in_analysis_layers():
    assert Layer.SECURITY == "I"
    assert "I" not in ANALYSIS_LAYERS and {"A", "B", "C", "D", "E"} <= set(ANALYSIS_LAYERS)


def test_specific_event_needs_narrative_and_stance(institution, narrative):
    with pytest.raises(ValidationError):
        event(institution, specificity="specific").full_clean()
    with pytest.raises(ValidationError):
        event(institution, specificity="specific", narrative=narrative).full_clean()
    with pytest.raises(ValidationError):  # obecná formulace se s narativem nemíchá
        event(institution, narrative=narrative, stance="contra").full_clean()

    specific = event(institution, specificity="specific", narrative=narrative, stance="contra")
    specific.full_clean()
    specific.save()
    assert specific.narrative_version == narrative.latest_version  # verze kódové knihy

    with pytest.raises(IntegrityError), transaction.atomic():
        event(institution, specificity="specific").save()  # i na úrovni databáze


def test_event_window_settings():
    assert (get_setting("event-window-before-days"), get_setting("event-window-after-days")) == (
        14,
        14,
    )


def test_import_security_sources_and_institutions():
    out = io.StringIO()
    call_command("import_sources", "apps/ingestion/sources/security.json", stdout=out)
    call_command("import_institutions", stdout=out)
    call_command("import_institutions", stdout=out)  # idempotentně

    assert Source.objects.filter(layer="I").count() == 3
    assert Institution.objects.count() == 10
    bis = Institution.objects.get(abbreviation="BIS")
    assert bis.sources.get().url == "https://www.bis.cz/aktuality/"
    assert (bis.actor.layer, bis.actor.actor_type) == ("I", Actor.Type.INSTITUTION)
    assert not Institution.objects.get(abbreviation="VZ").sources.exists()


def test_bis_date_from_text_pattern():
    sources = json.loads(Path("apps/ingestion/sources/security.json").read_text())["sources"]
    pattern = sources[0]["collector_config"]["date_regex"]
    html = "<p><strong>Publikováno: 3.6.2025</strong></p>"
    assert page_date(html, pattern) == datetime.datetime(2025, 6, 3, tzinfo=PRAGUE)
    assert page_date("<p>bez data</p>", pattern) is None


def test_listing_by_xpath_without_common_prefix():
    html = """<nav><a href="/o-nas">O nás</a></nav>
    <ul><li><h3><a href="/resort-ma-nove-generaly">Generálové</a></h3></li>
    <li><h3><a href="/ministr-jednal">Jednání</a></h3></li></ul>""".encode()
    entries = parse_listing(html, "https://mv.example/zpravodajstvi", xpath="//li//h3/a/@href")
    assert [e.url for e in entries] == [
        "https://mv.example/resort-ma-nove-generaly",
        "https://mv.example/ministr-jednal",
    ]


def test_actor_security_portfolio_and_document_speaking_role():
    actor = Actor.objects.create(name="Ministr vnitra", actor_type="politician", layer="E",
                                 security_portfolio=True)  # fmt: skip
    assert actor.security_portfolio
    from apps.documents.models import Document

    assert Document.SpeakingRole.INSTITUTIONAL == "institutional"
