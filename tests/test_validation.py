"""Валидация (этап 10): выборка, разметка вслепую, согласие, precision/recall, экспорт."""

import datetime

import pytest
from django.utils import timezone

from apps.actors.models import ActorInclusionPeriod
from apps.core.roles import assign_role
from apps.documents.models import DocumentQuality, ReuseLink
from apps.narratives.models import LabelDecision
from apps.panel.models import AuditLog
from apps.validation import sampling, stats
from apps.validation.models import Adjudication, Annotation, Review, Sample

from .factories import ActorFactory, DocumentFactory, NarrativeFactory, SourceFactory

pytestmark = pytest.mark.django_db

# --- статистика ----------------------------------------------------------------------------


def test_krippendorff_alpha_reference_example():
    """Krippendorff (2011), Computing Krippendorff's Alpha-Reliability: nominální α = 0,743."""
    units = [
        [1, 1, 1],
        [2, 2, 3, 2],
        [3, 3, 3, 3],
        [3, 3, 3, 3],
        [2, 2, 2, 2],
        [1, 2, 3, 4],
        [4, 4, 4, 4],
        [1, 1, 2, 1],
        [2, 2, 2, 2],
        [5, 5, 5],
        [1, 1],
        [3],
    ]
    assert stats.krippendorff_alpha(units) == pytest.approx(0.743, abs=0.001)
    assert stats.krippendorff_alpha([["a", "a"], ["b", "b"]]) == 1.0
    assert stats.krippendorff_alpha([["a"]]) is None
    assert stats.percent_agreement([[1, 1], [1, 2], [3]]) == 0.5


def test_wilson_interval():
    low, high = stats.wilson(8, 10)
    assert 0.49 < low < 0.5 and 0.94 < high < 0.95
    assert stats.wilson(0, 0) is None


def test_allocate_moves_shortfall_to_other_strata():
    shares = {"a": 0.5, "b": 0.5}
    assert sampling.allocate({"a": 3, "b": 100}, 20, shares) == {"a": 3, "b": 17}
    assert sampling.allocate({"a": 3, "b": 4}, 20, shares) == {"a": 3, "b": 4}


# --- данные --------------------------------------------------------------------------------


def eligible_doc(actor, **kwargs):
    d = DocumentFactory(actor=actor, published_at=timezone.now(), **kwargs)
    DocumentQuality.objects.create(
        document=d,
        score=5,
        quality_rules_version="test",
        has_title=True,
        has_published_at=True,
        date_plausible=True,
        text_ok=True,
        text_short=False,
        text_empty=False,
        lang_ok=True,
        boilerplate_dup=False,
        blocked_page=False,
        extraction_ratio_ok=True,
    )
    return d


def decide(document, narrative, present, stance="", model="m1"):
    return LabelDecision.objects.create(
        document=document,
        narrative=narrative,
        narrative_version=narrative.latest_version,
        text_hash=document.text_hash or "h",
        similarity=0.5,
        present=present,
        stance=stance,
        model_name=model,
        decided_at=timezone.now(),
    )


@pytest.fixture
def corpus():
    actor = ActorFactory()
    ActorInclusionPeriod.objects.create(actor=actor, start_date=datetime.date(2020, 1, 1))
    narrative = NarrativeFactory(code="valka")
    docs = {
        "pos": [eligible_doc(actor, is_relevant=True) for _ in range(4)],
        "cand": [eligible_doc(actor, is_relevant=True) for _ in range(6)],
        "rel": [eligible_doc(actor, is_relevant=True) for _ in range(5)],
        "other": [eligible_doc(actor, is_relevant=False) for _ in range(10)],
    }
    for d in docs["pos"]:
        decide(d, narrative, True, "pro")
    for d in docs["cand"]:
        decide(d, narrative, False)
    return narrative, docs


def test_narrative_sample_strata_weights_and_seed(corpus):
    narrative, docs = corpus
    shares = {"pozitivni": 0.4, "kandidat": 0.3, "relevantni": 0.2, "ostatni": 0.1}
    sample = sampling.create_narrative_sample(
        name="S", versions=[narrative.latest_version], size=10, seed=7, shares=shares
    )
    assert sample.params["population"] == {
        "pozitivni": 4,
        "kandidat": 6,
        "relevantni": 5,
        "ostatni": 10,
    }
    drawn = sample.params["drawn"]
    assert drawn == {"pozitivni": 4, "kandidat": 3, "relevantni": 2, "ostatni": 1}
    weights = dict(sample.items.values_list("stratum", "weight").distinct())
    assert weights["kandidat"] == pytest.approx(2.0) and weights["ostatni"] == pytest.approx(10.0)
    again = sampling.create_narrative_sample(
        name="S2", versions=[narrative.latest_version], size=10, seed=7, shares=shares
    )
    assert list(sample.items.values_list("document_id", flat=True)) == list(
        again.items.values_list("document_id", flat=True)
    )


# --- разметка и метрики --------------------------------------------------------------------


@pytest.fixture
def annotators(make_user):
    users = [make_user(f"anotator{i}") for i in (1, 2)]
    for user in users:
        assign_role(user, "annotator")
    return users


def annotate(sample, user, item, value=None, unclear=False, verdict=""):
    review = Review.objects.create(
        item=item, annotator=user, unclear=unclear, reuse_verdict=verdict
    )
    if value:
        version = sample.narrative_versions.get()
        Annotation.objects.create(review=review, narrative_version=version, value=value)
    return review


def test_evaluation_gold_adjudication_and_metrics(corpus, annotators, researcher):
    narrative, docs = corpus
    sample = sampling.create_narrative_sample(
        name="S", versions=[narrative.latest_version], size=25, seed=1
    )
    items = {i.document_id: i for i in sample.items.all()}
    a, b = annotators
    # 4 pozitivy modelu: 3 skutečně pro, 1 není (FP)
    for d in docs["pos"][:3]:
        annotate(sample, a, items[d.pk], "pro")
        annotate(sample, b, items[d.pk], "pro")
    annotate(sample, a, items[docs["pos"][3].pk], "absent")
    annotate(sample, b, items[docs["pos"][3].pk], "absent")
    # kandidát, který model zamítl, ale narativ v něm je (FN); spor vyřeší výzkumník
    missed = items[docs["cand"][0].pk]
    annotate(sample, a, missed, "contra")
    annotate(sample, b, missed, "absent")
    # nerelevantní dokument: shoda „není“ (TN)
    annotate(sample, a, items[docs["other"][0].pk], "absent")
    annotate(sample, b, items[docs["other"][0].pk], "absent")
    annotate(sample, a, items[docs["other"][1].pk], unclear=True)

    assert stats.disagreements(sample) == [(missed.pk, narrative.latest_version.pk)]
    result = stats.evaluate(sample)["results"][0]
    assert result["gold_items"] == 5
    assert result["presence"]["tp"] == 3 and result["presence"]["fp"] == 1
    assert result["presence"]["fn"] == 0 and result["presence"]["precision"] == 0.75
    Adjudication.objects.create(
        item=missed,
        narrative_version=narrative.latest_version,
        value="contra",
        decided_by=researcher,
    )
    result = stats.evaluate(sample)["results"][0]
    assert result["presence"]["fn"] == 1 and result["presence"]["recall"] == 0.75
    assert result["stance_accuracy"] == 1.0 and result["confusion"]["contra"] == {"absent": 1}
    # váhy: FN pochází z vrstvy „kandidát“ (6 v populaci / vybráno 6 = váha 1), TP z pozitivních
    assert result["presence_weighted"]["recall"] == pytest.approx(0.75)
    assert result["alpha"] is not None and result["items_with_2_annotations"] == 6
    # jiný model: jeho rozhodnutí nejsou → vše „není“
    other = stats.evaluate(sample, "jiny-model")["results"][0]
    assert other["presence"]["tp"] == 0


def test_pending_model_decision_is_excluded(corpus, annotators):
    narrative, docs = corpus
    sample = sampling.create_narrative_sample(
        name="S", versions=[narrative.latest_version], size=25, seed=1
    )
    item = sample.items.get(document=docs["rel"][0])
    LabelDecision.objects.create(
        document=item.document,
        narrative=narrative,
        narrative_version=narrative.latest_version,
        text_hash="x",
        similarity=0.4,
    )
    for user in annotators:
        annotate(sample, user, item, "absent")
    result = stats.evaluate(sample)["results"][0]
    assert result["pending_model"] == 1 and result["gold_items"] == 0


# --- интерфейс разметки --------------------------------------------------------------------


def test_annotation_is_blind_and_saves(client, corpus, annotators):
    narrative, docs = corpus
    sample = sampling.create_narrative_sample(
        name="Pilot", versions=[narrative.latest_version], size=5, seed=3
    )
    a, b = annotators
    first = sample.items.get(position=1)
    annotate(sample, b, first, "contra")
    client.force_login(a)
    assert client.get("/").url == "/validace/"
    assert "Pilot" in client.get("/validace/").content.decode()
    assert client.get(f"/validace/{sample.pk}/").url == f"/validace/{sample.pk}/polozka/1/"
    page = client.get(f"/validace/{sample.pk}/polozka/1/").content.decode()
    assert narrative.name in page
    assert first.document.source.name not in page  # zdroj skryt
    assert "checked" not in page  # odpověď jiného anotátora ani model nejsou vidět
    missing = client.post(f"/validace/{sample.pk}/polozka/1/", {}).content.decode()
    assert "Chybí hodnota" in missing
    version = narrative.latest_version
    response = client.post(f"/validace/{sample.pk}/polozka/1/", {f"v{version.pk}": "pro"})
    assert response.url == f"/validace/{sample.pk}/"
    assert Review.objects.get(item=first, annotator=a).annotations.get().value == "pro"
    client.post(f"/validace/{sample.pk}/polozka/1/", {f"v{version.pk}": "absent", "note": "x"})
    assert Review.objects.get(item=first, annotator=a).annotations.get().value == "absent"
    sample.status = Sample.Status.CLOSED
    sample.save()
    client.post(f"/validace/{sample.pk}/polozka/1/", {f"v{version.pk}": "pro"})
    assert Review.objects.get(item=first, annotator=a).annotations.get().value == "absent"
    assert client.get("/panel/validace/").status_code == 403


def test_observer_cannot_annotate(observer_client):
    assert observer_client.get("/validace/").status_code == 403


# --- заимствования -------------------------------------------------------------------------


def test_reuse_sample_and_predictions(annotators):
    actor = ActorFactory()
    ActorInclusionPeriod.objects.create(actor=actor, start_date=datetime.date(2020, 1, 1))
    a_source, d_source = SourceFactory(layer="A"), SourceFactory(layer="D")
    pairs = []
    for link_type in ["verbatim", "verbatim", "paraphrase"]:
        s = eligible_doc(actor, source=a_source)
        t = eligible_doc(ActorFactory(), source=d_source)
        ReuseLink.objects.create(
            source_document=s,
            target_document=t,
            link_type=link_type,
            method="minhash",
            similarity=0.9,
        )
        pairs.append((s, t))
    sample = sampling.create_reuse_sample(name="P", size=10, seed=1)
    assert sample.params["population"] == {"doslovne": 2, "parafraze": 1}
    assert sample.items.count() == 3  # bez embeddingů žádné „blízké dvojice“
    predicted = stats.reuse_predictions(sample)
    assert sorted(predicted.values()) == ["paraphrase", "verbatim", "verbatim"]
    a, b = annotators
    for item in sample.items.all():
        verdict = "none" if item.stratum == "parafraze" else "verbatim"
        annotate(sample, a, item, verdict=verdict)
        annotate(sample, b, item, verdict=verdict)
    result = stats.evaluate(sample)["results"][0]
    assert result["presence"]["tp"] == 2 and result["presence"]["fp"] == 1
    assert result["alpha"] == 1.0


# --- панель исследователя ------------------------------------------------------------------


def test_panel_create_sample_adjudicate_export(researcher_client, corpus, annotators):
    narrative, docs = corpus
    response = researcher_client.post(
        "/panel/validace/",
        {
            "name": "Hlavní vzorek",
            "kind": "narratives",
            "versions": [narrative.latest_version.pk],
            "size": 12,
            "seed": 5,
            "hide_source": "on",
        },
    )
    sample = Sample.objects.get(name="Hlavní vzorek")
    assert response.url == f"/panel/validace/{sample.pk}/" and sample.items.count() == 12
    assert AuditLog.objects.filter(content_type__model="sample", action="create").exists()
    a, b = annotators
    item = sample.items.get(position=1)
    annotate(sample, a, item, "pro")
    annotate(sample, b, item, "absent")
    page = researcher_client.get(f"/panel/validace/{sample.pk}/").content.decode()
    assert "Spory k rozhodnutí (1)" in page
    dispute = researcher_client.get(f"/panel/validace/{sample.pk}/spor/{item.pk}/").content.decode()
    assert "A1 (anotator1)" in dispute and "model" in dispute
    version = narrative.latest_version
    researcher_client.post(
        f"/panel/validace/{sample.pk}/spor/{item.pk}/",
        {f"k{version.pk}": "pro", "note": "výslovně"},
    )
    assert Adjudication.objects.get(item=item).value == "pro"
    assert AuditLog.objects.filter(action="decision").count() == 1
    csv = researcher_client.get(f"/panel/validace/{sample.pk}/export.csv").content.decode()
    assert "A1" in csv and "anotator1" not in csv and "rozhodnutí" in csv
    researcher_client.post(f"/panel/validace/{sample.pk}/", {"action": "report", "note": "pilot"})
    report = sample.reports.get()
    data = researcher_client.get(
        f"/panel/validace/{sample.pk}/export.json", {"report": report.pk}
    ).json()
    assert data["results"][0]["narrative"] == "valka" and data["sample"]["params"]["seed"] == 5
    researcher_client.post(f"/panel/validace/{sample.pk}/", {"action": "close"})
    sample.refresh_from_db()
    assert sample.status == Sample.Status.CLOSED


def test_validation_report_command(corpus):
    from io import StringIO

    from django.core.management import call_command

    narrative, _docs = corpus
    sample = sampling.create_narrative_sample(
        name="CLI", versions=[narrative.latest_version], size=5, seed=1
    )
    out = StringIO()
    call_command("validation_report", sample.pk, stdout=out)
    assert "valka v1" in out.getvalue()
    out = StringIO()
    call_command("validation_report", sample.pk, "--json", stdout=out)
    assert '"kind": "narratives"' in out.getvalue()
