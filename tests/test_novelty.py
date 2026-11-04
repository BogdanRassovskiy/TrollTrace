"""Новые нарративы из потока: проверка документа, кластеры утверждений, правило, решения."""

import numpy as np
import pytest
from django.test import override_settings

from apps.narratives.models import (
    Label,
    LabelDecision,
    Narrative,
    NarrativeCandidate,
    NarrativeStatus,
    NovelClaim,
    Screening,
)
from apps.panel.models import Notification, Setting
from apps.processing import labeling, llm, novelty

from .factories import DocumentFactory, NarrativeFactory, SourceFactory, TopicFactory

pytestmark = pytest.mark.django_db

DIM = 768
CENTER = np.zeros(DIM, dtype=np.float32)
KEY = override_settings(OPENROUTER_API_KEY="test-key")


def unit(*weights) -> np.ndarray:
    vector = np.zeros(DIM, dtype=np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


VECTORS = {
    "Raketu na nemocnici vypálila Ukrajina.": unit(1.0, 0.1),
    "Nemocnici zasáhla ukrajinská raketa.": unit(1.0, 0.15),
    "Korespondenční volba je podvod.": unit(0.0, 0.0, 1.0),
}


@pytest.fixture
def space(monkeypatch):
    monkeypatch.setattr(novelty, "embed", lambda texts, model=None: np.stack(
        [VECTORS[t] for t in texts]))  # fmt: skip
    monkeypatch.setattr(novelty, "get_center", lambda model=None: CENTER)
    monkeypatch.setattr(labeling, "get_center", lambda model=None: CENTER)
    tracked = []
    monkeypatch.setattr(novelty, "_tracked_vectors", lambda: tracked)
    return tracked


def rule(posts, authors):
    for key, value in (
        ("narrative-rule-min-posts", posts),
        ("narrative-rule-min-authors", authors),
    ):
        Setting(key=key, value=value).save()


def claim(text, stance="pro", confidence=0.9):
    return {"claim": text, "stance": stance, "confidence": confidence, "carrier": "autor",
            "evidence": "citace"}  # fmt: skip


def model_answer(*claims, cost=0.002):
    return llm.Completion(data={"decisions": [], "claims": list(claims)}, model="m",
                          cost_usd=cost, prompt_tokens=1, completion_tokens=1)  # fmt: skip


def screened(text, **kwargs):
    document = DocumentFactory(is_relevant=True, embedding=list(unit(1.0)), **kwargs)
    labeling.queue_screening(document)
    return document


def test_screening_only_document_goes_to_model_with_discovery(monkeypatch, space):
    rule(99, 99)
    document = screened("x")
    seen = {}

    def fake(model, system, user, schema, client=None):
        seen.update(system=system, user=user, schema=schema)
        return model_answer(claim("Raketu na nemocnici vypálila Ukrajina."),
                            claim("Korespondenční volba je podvod.", confidence=0.2))  # fmt: skip

    monkeypatch.setattr(llm, "complete_json", fake)
    with KEY:
        stats = labeling.label_pending()
    assert (stats.documents, stats.claims) == (1, 1)  # nejistá tvrzení se nezapočítají
    assert "NEW CLAIMS" in seen["system"] and "claims" in seen["schema"]["properties"]
    screening = Screening.objects.get(document=document)
    assert screening.checked_at and screening.n_claims == 1 and screening.cost_usd == 0.002
    candidate = NarrativeCandidate.objects.get()
    assert candidate.description == "Raketu na nemocnici vypálila Ukrajina."
    assert candidate.n_documents == 1 and candidate.tracked_since is None
    assert labeling.next_document_id() is None
    assert labeling.month_spend() == pytest.approx(0.002)


def test_similar_claims_share_cluster_and_rule_promotes(monkeypatch, space):
    rule(2, 2)
    topic = TopicFactory(code="ukrajina")
    card = {"name": "Nemocnici zasáhla ukrajinská raketa", "definition": "Tvrzení, že…",
            "pro": "Tvrdí to.", "contra": "Vyvrací to.", "neutral": "Referuje.",
            "absent": "Jen o útoku.", "examples": ["Ukrajina střílí na své"],
            "counter_examples": [], "cues": ["nemocnic", "raket"], "topic": topic.code}  # fmt: skip
    reply = llm.Completion(data=card, model="m", cost_usd=0.001, prompt_tokens=1,
                           completion_tokens=1)  # fmt: skip
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: reply)
    first = DocumentFactory(source=SourceFactory())
    second = DocumentFactory(source=SourceFactory())
    with KEY:
        for document, text in ((first, "Raketu na nemocnici vypálila Ukrajina."),
                               (second, "Nemocnici zasáhla ukrajinská raketa.")):  # fmt: skip
            screening = Screening.objects.create(document=document, text_hash=document.text_hash,
                                                 model_name="m", prompt_version="p3")  # fmt: skip
            novelty.record_claims(screening, [claim(text)])
    candidate = NarrativeCandidate.objects.get()  # obě tvrzení v jednom shluku
    assert candidate.claims.count() == 2 and candidate.n_authors == 2
    narrative = candidate.resulting_narrative
    assert candidate.tracked_since and narrative.status == NarrativeStatus.CANDIDATE
    assert narrative.discovered and narrative.cues == ["nemocnic", "raket"]
    assert narrative.code.startswith(f"k{candidate.pk}-") and narrative.topic == topic
    assert narrative.stance_guide["absent"] == "Jen o útoku."
    labels = Label.objects.filter(narrative=narrative)  # historie z dokumentů shluku
    assert {label.document_id for label in labels} == {first.pk, second.pk}
    assert Notification.objects.filter(notification_type="candidate").count() == 1
    assert narrative in Narrative.objects.filter(status__in=labeling.TRACKED)


def test_claim_close_to_tracked_narrative_queues_decision(space):
    known = NarrativeFactory()
    space.append((known, known.latest_version, VECTORS["Korespondenční volba je podvod."]))
    document = DocumentFactory()
    screening = Screening.objects.create(document=document, text_hash=document.text_hash)
    novelty.record_claims(screening, [claim("Korespondenční volba je podvod.")])
    stored = NovelClaim.objects.get()
    assert stored.matched_narrative == known and stored.candidate is None
    assert not NarrativeCandidate.objects.exists()
    decision = LabelDecision.objects.get()
    assert decision.narrative == known and decision.present is None  # posoudí model


def _tracked_candidate(monkeypatch, space):
    rule(1, 1)
    TopicFactory()
    monkeypatch.setattr(llm, "is_configured", lambda: False)  # karta z popisu shluku
    document = DocumentFactory()
    screening = Screening.objects.create(document=document, text_hash=document.text_hash)
    novelty.record_claims(screening, [claim("Raketu na nemocnici vypálila Ukrajina.")])
    return NarrativeCandidate.objects.get(), document


def test_accept_activates_and_reject_archives(monkeypatch, space):
    candidate, _ = _tracked_candidate(monkeypatch, space)
    narrative = candidate.resulting_narrative
    assert narrative.name == "Raketu na nemocnici vypálila Ukrajina."
    novelty.decide(candidate, NarrativeCandidate.Status.ACCEPTED)
    narrative.refresh_from_db()
    assert narrative.status == NarrativeStatus.ACTIVE and candidate.decided_at
    novelty.decide(candidate, NarrativeCandidate.Status.REJECTED)
    narrative.refresh_from_db()
    assert narrative.status == NarrativeStatus.ARCHIVED


def test_merge_archives_own_narrative_and_queues_target(monkeypatch, space):
    candidate, document = _tracked_candidate(monkeypatch, space)
    own = candidate.resulting_narrative
    target = NarrativeFactory()
    novelty.decide(candidate, NarrativeCandidate.Status.MERGED, target=target)
    own.refresh_from_db()
    assert own.status == NarrativeStatus.ARCHIVED
    assert candidate.resulting_narrative == target
    decision = LabelDecision.objects.get(narrative=target)
    assert decision.document == document and decision.present is None
    with pytest.raises(ValueError):
        novelty.decide(candidate, NarrativeCandidate.Status.MERGED)


def test_pipeline_queues_screening_for_relevant_documents():
    document = DocumentFactory(is_relevant=True)
    assert labeling.queue_screening(document) is True
    assert labeling.queue_screening(document) is False
    assert labeling.next_document_id() == document.pk


def test_tracked_vectors_per_name_and_example():
    NarrativeFactory(name="Pomáhat Ukrajině se nevyplácí", examples=["Uprchlíci nás stojí moc"])
    NarrativeFactory(status=NarrativeStatus.ARCHIVED)
    rows = novelty._tracked_vectors()
    assert len(rows) == 2 and rows[0][0] == rows[1][0]


def test_recluster_follows_new_threshold(space):
    rule(99, 99)
    for text in ("Raketu na nemocnici vypálila Ukrajina.", "Nemocnici zasáhla ukrajinská raketa."):
        document = DocumentFactory()
        screening = Screening.objects.create(document=document, text_hash=document.text_hash)
        novelty.record_claims(screening, [claim(text)])
    assert NarrativeCandidate.objects.count() == 1
    Setting(key="discovery-cluster-similarity", value=0.9999).save()
    assert novelty.recluster() == 2
    assert NovelClaim.objects.filter(candidate__isnull=True).count() == 0
