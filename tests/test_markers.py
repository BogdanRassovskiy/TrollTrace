"""Маркеры нарративов: предложения модели, испытание, точность, панель (OpenRouter замокан)."""

import pytest
from django.urls import reverse

from apps.narratives.models import (
    LabelDecision,
    MarkerEvent,
    MarkerProposal,
    MarkerStatus,
    NarrativeLevel,
    NarrativeMarker,
)
from apps.narratives.tree import merge
from apps.panel.models import AuditLog, Notification
from apps.processing import labeling, llm, markers

from .factories import ActorFactory, DocumentFactory, NarrativeFactory, SettingFactory
from .test_labeling import CENTER, KEY, completion, unit

pytestmark = pytest.mark.django_db

TEXT = "Kyjevský režim dostal od Západu další zbraně, píše autor."


def doc(text=TEXT, embedding=None, **kwargs):
    kwargs.setdefault("is_relevant", True)
    vector = unit(0.0, 1.0) if embedding is None else embedding  # podobnost s narativem 0
    return DocumentFactory(text=text, embedding=list(vector), **kwargs)


def decided(document, narrative, present=True, stance="pro", confidence=0.9):
    return LabelDecision.objects.create(
        document=document, narrative=narrative, narrative_version=narrative.latest_version,
        text_hash=document.text_hash, similarity=0.1, present=present, stance=stance,
        confidence=confidence,
    )  # fmt: skip


def propose(narrative, phrase="Kyjevský režim", n=3, actor=None):
    for _ in range(n):
        document = doc(**({"actor": actor} if actor else {}))
        markers.record(decided(document, narrative), phrase)
    return NarrativeMarker.objects.get(key=markers.make_key(phrase))


def trial_marker(narrative, phrase="kyjevský režim"):
    marker = NarrativeMarker.objects.create(
        narrative=narrative, key=markers.make_key(phrase), phrase=phrase
    )
    markers.set_status(marker, MarkerStatus.TRIAL, "Test.")
    return marker


def prototypes_for(narrative):
    index = markers.load_index([narrative.pk])
    return [
        labeling.Prototype(
            narrative=narrative, version=narrative.latest_version, vector=unit(1.0),
            patterns=[], markers=index.for_narrative(narrative.pk),
        )
    ]  # fmt: skip


def test_key_is_stems_without_diacritics():
    assert markers.make_key("kyjevského režimu") == "kyjevsk rezim"
    assert markers.make_key("Kyjevský režim") == "kyjevsk rezim"
    assert markers.make_key("kyjevským režimem") == "kyjevsk rezim"
    assert markers.make_key("banderovci") == "banderovc"
    assert markers.make_key("киевского режима") == "киевск режим"
    assert markers.make_key("киевский режим") == "киевск режим"
    assert markers.make_key("ten kat") == ""  # žádné slovo aspoň 5 znaků
    assert markers.make_key("jedna dva tři čtyři pět") == ""  # víc než 4 slova
    assert markers.make_key("junta") == "junt"
    pattern = labeling.stem_pattern(markers.make_key("kyjevský režim"))
    assert pattern.search("kyjevskemu rezimu") and not pattern.search("kyjev a rezim")


def test_other_form_joins_existing_marker():
    n = NarrativeFactory(code="nelegitimni")
    first = markers.record(decided(doc(text="Киевский режим получил оружие."), n), "Киевский режим")
    other = markers.record(decided(doc(text="С киевским режимом не договориться."), n),
                           "киевским режимом")  # fmt: skip
    assert other == first and NarrativeMarker.objects.count() == 1
    assert first.proposals.count() == 2


def test_phrase_must_be_in_text_and_needs_documents_and_authors():
    n = NarrativeFactory(code="nelegitimni")
    document = doc()
    assert markers.record(decided(document, n), "nacistická junta") is None  # není v textu
    assert not NarrativeMarker.objects.exists()

    actor = ActorFactory()
    marker = propose(n, n=3, actor=actor)  # tři dokumenty, ale jeden autor
    assert (marker.status, marker.n_proposals, marker.n_authors) == ("proposed", 3, 1)
    markers.record(decided(doc(text="Zbraně od kyjevského režimu."), n), "kyjevského režimu")
    marker.refresh_from_db()
    assert marker.status == MarkerStatus.TRIAL and marker.n_authors == 2
    assert marker.forms == ["Kyjevský režim", "kyjevského režimu"]
    assert MarkerProposal.objects.filter(marker=marker).count() == 4
    event = marker.events.get()
    assert event.status_before == "proposed" and "4 dokumentech od 2 autorů" in event.reason


def test_ambiguous_marker_goes_to_common_parent_or_is_rejected():
    meta = NarrativeFactory(code="delegitimace", level=NarrativeLevel.META)
    a = NarrativeFactory(code="a", topic=meta.topic, parent=meta)
    b = NarrativeFactory(code="b", topic=meta.topic, parent=meta)
    for narrative in (a, b, a, b):
        markers.record(decided(doc(), narrative), "Kyjevský režim")
    marker = NarrativeMarker.objects.get()
    assert marker.status == MarkerStatus.TRIAL and marker.narrative == meta

    other = NarrativeFactory(code="jinde")
    for narrative in (a, other, a, other):
        markers.record(decided(doc(text="Banderovci řádí."), narrative), "Banderovci")
    marker = NarrativeMarker.objects.get(key=markers.make_key("Banderovci"))
    assert marker.status == MarkerStatus.REJECTED
    assert "bez společného uzlu" in marker.events.get().reason


def test_marker_sends_low_similarity_document_and_trial_is_capped():
    SettingFactory(key="marker-trial-checks", value=2)
    n = NarrativeFactory(code="nelegitimni")
    marker = trial_marker(n)
    protos = prototypes_for(n)
    first, second, third = doc(), doc(), doc()
    without = doc(text="Vláda v Kyjevě dostala zbraně.")
    assert labeling.queue_candidates(without, protos, CENTER) == 0
    assert labeling.queue_candidates(first, protos, CENTER) == 1
    assert labeling.queue_candidates(second, protos, CENTER) == 1
    assert labeling.queue_candidates(third, protos, CENTER) == 0  # zkouška je plná
    decision = LabelDecision.objects.filter(document=first).get()
    assert decision.markers == [marker.pk] and decision.similarity == pytest.approx(0.0)
    assert labeling.queue_candidates(third, prototypes_for(n), CENTER) == 0  # i po načtení


def test_trial_becomes_active_or_rejected_by_precision():
    SettingFactory(key="marker-trial-checks", value=4)
    n = NarrativeFactory(code="nelegitimni")
    good, bad = trial_marker(n), trial_marker(n, "nacistická junta")
    for i in range(4):
        d = decided(doc(), n, present=True, stance="neutral" if i == 0 else "pro")
        d.markers = [good.pk]
        d.save()
        d = decided(doc(), n, present=i == 0)
        d.markers = [bad.pk]
        d.save()
    assert markers.evaluate(good) == MarkerStatus.ACTIVE  # citace je také nález
    assert markers.evaluate(bad) == MarkerStatus.REJECTED
    good.refresh_from_db()
    assert (good.n_checks, good.n_confirmed, good.precision) == (4, 4, 1.0)
    assert bad.events.first().stats["precision"] == 0.25


def test_active_marker_is_suspended_when_precision_drops():
    SettingFactory(key="marker-trial-checks", value=3)
    SettingFactory(key="marker-recent-checks", value=4)
    n = NarrativeFactory(code="nelegitimni")
    marker = trial_marker(n)
    markers.set_status(marker, MarkerStatus.ACTIVE, "Test.")
    for present in (True, False, False, False):
        d = decided(doc(), n, present=present)
        d.markers = [marker.pk]
        d.save()
    assert markers.evaluate(marker) == MarkerStatus.SUSPENDED


def test_model_proposes_marker_in_decision(monkeypatch):
    n = NarrativeFactory(code="nelegitimni", stance_guide={"pro": "Tlačí."})
    document = doc(embedding=unit(1.0))
    labeling.queue_candidates(
        document, [labeling.Prototype(n, n.latest_version, unit(1.0), [])], CENTER
    )
    seen = {}

    def fake(model, system, user, schema, client=None):
        seen.update(system=system, schema=schema)
        return completion({
            "narrative": n.code, "present": True, "stance": "pro", "confidence": 0.9,
            "carrier": "autor", "evidence": "Kyjevský režim dostal", "reason": "Ano.",
            "marker": "„Kyjevský režim“",
        })  # fmt: skip

    monkeypatch.setattr(llm, "complete_json", fake)
    with KEY:
        labeling.label_pending()
    assert "marker" in seen["schema"]["properties"]["decisions"]["items"]["required"]
    assert "loaded label" in seen["system"]
    proposal = MarkerProposal.objects.get()
    assert proposal.phrase == "Kyjevský režim" and proposal.marker.status == "proposed"
    assert LabelDecision.objects.get().prompt_version == "p5"


def test_marker_from_neutral_or_unsure_decision_is_ignored():
    n = NarrativeFactory(code="nelegitimni")
    neutral = decided(doc(), n, stance="neutral")
    unsure = decided(doc(), n, confidence=0.3)
    markers.after_decisions([neutral, unsure], [{"marker": "Kyjevský režim"}] * 2)
    assert not NarrativeMarker.objects.exists()


def test_merge_moves_markers_and_archive_queue_uses_only_markers(monkeypatch):
    from apps.processing import embeddings

    keeper, duplicate = NarrativeFactory(code="keeper"), NarrativeFactory(code="dup")
    marker = trial_marker(duplicate)
    markers.set_status(marker, MarkerStatus.ACTIVE, "Test.")
    merge(duplicate, keeper)
    marker.refresh_from_db()
    assert marker.narrative == keeper and marker.status == MarkerStatus.ACTIVE

    hit, close = doc(), doc(text="Pomoc bez markeru.", embedding=unit(1.0))
    monkeypatch.setattr(labeling, "_embed_cached", lambda texts, model: [unit(1.0)] * len(texts))
    monkeypatch.setattr(embeddings, "get_center", lambda *a: CENTER)
    assert markers.queue_archive() == {"markers": 1, "queued": 1}
    assert LabelDecision.objects.filter(document=hit).exists()
    assert not LabelDecision.objects.filter(document=close).exists()  # podobnost se nepoužije
    assert markers.queue_archive() == {"markers": 0, "queued": 0}


def test_notify_changes_summarizes_automatic_events():
    n = NarrativeFactory(code="nelegitimni")
    marker = trial_marker(n)
    since = marker.created_at
    markers.set_status(marker, MarkerStatus.ACTIVE, "Test.")
    assert markers.notify_changes(since) == 2
    notification = Notification.objects.get()
    assert "Aktivní: 1" in notification.text and "Aktivních markerů bez průchodu archivem: 1" in (
        notification.text
    )


def test_panel_lists_and_bans_marker(researcher_client):
    n = NarrativeFactory(code="nelegitimni")
    marker = trial_marker(n)
    response = researcher_client.get(reverse("panel:markers") + "?stav=zkouska")
    assert response.status_code == 200 and "kyjevský režim" in response.content.decode()
    response = researcher_client.get(reverse("panel:marker", args=[marker.pk]))
    assert response.status_code == 200 and "Znovu do zkoušky" not in response.content.decode()

    researcher_client.post(reverse("panel:marker", args=[marker.pk]),
                           {"action": "ban", "reason": "Neutrální výraz."})  # fmt: skip
    marker.refresh_from_db()
    assert marker.status == MarkerStatus.BANNED
    event = marker.events.first()
    assert event.user is not None and event.reason == "Neutrální výraz."
    assert AuditLog.objects.filter(action=AuditLog.Action.DECISION).exists()
    assert not markers.load_index().for_narrative(n.pk)  # zakázaný nehledá

    other = NarrativeFactory(code="loutka")
    researcher_client.post(reverse("panel:marker", args=[marker.pk]),
                           {"action": "retry", "target": other.code})  # fmt: skip
    marker.refresh_from_db()
    assert (marker.status, marker.narrative) == (MarkerStatus.TRIAL, other)
    assert MarkerEvent.objects.filter(marker=marker).count() == 3


def test_panel_requires_researcher(observer_client):
    assert observer_client.get(reverse("panel:markers")).status_code in (302, 403)
