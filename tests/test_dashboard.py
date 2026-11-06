"""Рабочая часть (этап 8): страницы, карточка нарратива, просмотр заимствований."""

import datetime

import pytest
from django.utils import timezone

from apps.actors.models import ActorInclusionPeriod, Layer
from apps.analytics import aggregation
from apps.analytics.models import WeeklyAggregate
from apps.dashboard.reuse import highlight
from apps.dashboard.views import lifecycle
from apps.documents.models import DocumentQuality, ReuseLink
from apps.narratives.models import Debunk, NarrativeStatus

from .factories import ActorFactory, DocumentFactory, LabelFactory, NarrativeFactory

pytestmark = pytest.mark.django_db

WEEK = datetime.date(2026, 10, 12)


def actor(**kwargs):
    a = ActorFactory(**kwargs)
    ActorInclusionPeriod.objects.create(actor=a, start_date=datetime.date(2026, 1, 1))
    return a


def at(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(10)))


def doc(who, day):
    d = DocumentFactory(actor=who, published_at=at(day))
    DocumentQuality.objects.create(
        document=d, score=5, quality_rules_version="test", has_title=True, has_published_at=True,
        date_plausible=True, text_ok=True, text_short=False, text_empty=False, lang_ok=True,
        boilerplate_dup=False, blocked_page=False, extraction_ratio_ok=True,
    )  # fmt: skip
    return d


def test_actor_list_filters_and_spikes(observer_client):
    n = NarrativeFactory()
    mp = actor(name="Jana Nováková", layer=Layer.PARLIAMENT, party="ANO", role="poslankyně")
    actor(name="Petr Dvořák", layer=Layer.PARLIAMENT, party="ODS")
    WeeklyAggregate.objects.create(actor=mp, narrative=n, week=WEEK, n_documents=3, n_total=4,
                                   share=0.75, z_score=3.1)  # fmt: skip
    page = observer_client.get("/dashboard/", {"party": "ANO"}).content.decode()
    assert "Jana Nováková" in page and "Petr Dvořák" not in page
    assert n.name in page  # výkyv
    assert "Petr Dvořák" in observer_client.get("/dashboard/", {"q": "dvoř"}).content.decode()
    assert "Jana" not in observer_client.get("/dashboard/", {"role": "ministr"}).content.decode()


def test_actor_page_and_api(observer_client):
    n = NarrativeFactory(code="pomoc")
    a = actor(layer=Layer.ALTERNATIVE)
    LabelFactory(document=doc(a, WEEK), narrative=n, stance="pro")
    aggregation.update(full=True)
    assert observer_client.get(f"/dashboard/akter/{a.pk}/").status_code == 200
    data = observer_client.get("/api/actor/", {"actor": a.pk}).json()
    assert data["series"][0]["code"] == "pomoc" and data["series"][0]["n"] == [1]
    assert data["spike_z"] == 2.0
    pro = observer_client.get("/api/actor/", {"actor": a.pk, "stance": "contra"}).json()
    assert pro["series"][0]["n"] == [0]
    assert observer_client.get("/api/actor/", {"actor": "x"}).status_code == 400


def test_compare_page_renders_config(observer_client):
    NarrativeFactory(code="b-narativ")
    NarrativeFactory(code="a-narativ")
    page = observer_client.get("/dashboard/srovnani/").content.decode()
    assert '"narrative": "a-narativ"' in page and '"click_max_lines": 10' in page
    page = observer_client.get("/dashboard/srovnani/", {"narrative": "b-narativ"}).content.decode()
    assert '"narrative": "b-narativ"' in page


def test_narrative_card_lifecycle(observer_client):
    n = NarrativeFactory(code="pomoc", stance_guide={"absent": "Jen téma."})
    Debunk.objects.create(narrative=n, organization="demagog", url="https://demagog.example/1",
                          title="Vyvrácení")  # fmt: skip
    alt = actor(layer=Layer.ALTERNATIVE, name="Web A")
    mp = actor(layer=Layer.PARLIAMENT, name="Poslanec")
    first = doc(alt, WEEK)
    later = doc(mp, WEEK + datetime.timedelta(days=9))
    LabelFactory(document=later, narrative=n, stance="pro")
    LabelFactory(document=first, narrative=n, stance="pro")
    aggregation.update(full=True)
    life = lifecycle(n)
    assert life["first"]["document"] == first
    assert [layer["code"] for layer in life["layers"]] == ["B", "E"]
    assert [a.name for a, _ in life["pickup"]] == ["Web A", "Poslanec"]
    assert life["peak"] is not None
    page = observer_client.get("/dashboard/narativy/pomoc/").content.decode()
    assert "První fixace" in page and "Vyvrácení" in page and "Jen téma." in page
    assert "pomoc" in observer_client.get("/dashboard/narativy/").content.decode()
    n.status = NarrativeStatus.ARCHIVED
    n.save()
    assert observer_client.get("/dashboard/narativy/pomoc/").status_code == 404


def test_reuse_highlight_ignores_case_and_diacritics():
    source = "Vláda tají pravdu o válce. Úplně jiný text tady."
    target = "Podle webu VLADA TAJI PRAVDU O VALCE, píše autor."
    left, right, share = highlight(source, target, min_words=5)
    assert ("Vláda tají pravdu o válce", True) in left
    assert ("VLADA TAJI PRAVDU O VALCE", True) in right
    assert share == pytest.approx(5 / 9)
    assert highlight("a b c", "d e f")[2] == 0


def test_reuse_pages(observer_client):
    a = DocumentFactory(text="Jedna dva tři čtyři pět šest sedm.")
    b = DocumentFactory(text="Úvod. Jedna dva tři čtyři pět šest sedm. Konec.")
    link = ReuseLink.objects.create(source_document=a, target_document=b, link_type="verbatim",
                                    method="minhash", similarity=0.9)  # fmt: skip
    assert "porovnat" in observer_client.get("/dashboard/prevzeti/").content.decode()
    page = observer_client.get(f"/dashboard/prevzeti/{link.pk}/").content.decode()
    assert "<mark>Jedna dva tři čtyři pět šest sedm</mark>" in page
    empty = observer_client.get("/dashboard/prevzeti/", {"type": "paraphrase"}).content.decode()
    assert "Zatím nebylo nalezeno" in empty
    # Фильтр по слою оригинала: направление не задано гипотезой, его выбирает исследователь.
    layer = a.source.layer
    other = next(code for code in Layer.values if code != layer)
    by_source = observer_client.get("/dashboard/prevzeti/", {"source_layer": layer})
    assert "porovnat" in by_source.content.decode()
    none = observer_client.get("/dashboard/prevzeti/", {"source_layer": other}).content.decode()
    assert "Zatím nebylo nalezeno" in none


@pytest.mark.parametrize("url", ["/dashboard/", "/dashboard/srovnani/", "/dashboard/narativy/",
                                 "/dashboard/prevzeti/"])  # fmt: skip
def test_pages_require_login(client, url):
    assert client.get(url).status_code == 302


@pytest.mark.parametrize("path", ["srovnani/", "akter/{pk}/"])
def test_share_link_button(observer_client, path):
    NarrativeFactory()
    a = actor()
    page = observer_client.get("/dashboard/" + path.format(pk=a.pk)).content.decode()
    assert 'id="share-link"' in page and "Kopírovat odkaz s nastavením" in page


def test_login_keeps_shared_settings(client):
    response = client.get("/dashboard/srovnani/", {"narrative": "x", "layers": "A,B", "pin": "3"})
    assert response.status_code == 302
    assert "srovnani/%3Fnarrative%3Dx%26layers%3DA%252CB%26pin%3D3" in response["Location"]
