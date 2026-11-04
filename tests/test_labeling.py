"""Разметка нарративов: кандидаты, решения модели (OpenRouter замокан), ретроразметка."""

import datetime
import io
import json

import httpx
import numpy as np
import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.narratives.models import Label, LabelDecision, Narrative, Topic
from apps.panel.models import Job, Notification
from apps.processing import labeling, llm

from .factories import DocumentFactory, NarrativeFactory

pytestmark = pytest.mark.django_db

DIM = 768
KEY = override_settings(OPENROUTER_API_KEY="test-key")


def unit(*weights) -> np.ndarray:
    vector = np.zeros(DIM, dtype=np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


def narrative(code="pomoc", cues=("pomoc ukrajin", "nevypla")):
    guide = {"pro": "Tlačí narativ.", "contra": "Vyvrací ho."}
    return NarrativeFactory(code=code, name=f"Narativ {code}", cues=list(cues), stance_guide=guide)


def prototype(n: Narrative, vector) -> labeling.Prototype:
    return labeling.Prototype(
        narrative=n,
        version=n.latest_version,
        vector=vector,
        patterns=[(c, labeling.stem_pattern(c)) for c in n.cues],
    )


def doc(text="Pomoc Ukrajině se nevyplácí, říkají kritici.", embedding=None, **kwargs):
    return DocumentFactory(
        text=text, is_relevant=True, embedding=list(unit(1.0) if embedding is None else embedding),
        **kwargs,
    )  # fmt: skip


def completion(*decisions, cost=0.002):
    return llm.Completion(
        data={"decisions": list(decisions)}, model="m", cost_usd=cost,
        prompt_tokens=100, completion_tokens=20,
    )  # fmt: skip


def answer(code, present=True, stance="pro", confidence=0.9):
    return {"narrative": code, "present": present, "stance": stance, "confidence": confidence,
            "carrier": "autor", "evidence": "Pomoc Ukrajině se nevyplácí",
            "reason": "Tvrdí to."}  # fmt: skip


CENTER = np.zeros(DIM, dtype=np.float32)


def test_candidates_by_similarity_or_cues():
    close, cued, far = narrative("a", cues=()), narrative("b"), narrative("c")
    document = doc()
    protos = [
        prototype(close, unit(1.0)),  # podobnost 1
        prototype(cued, unit(0.2, 0.98)),  # podobnost ≈0,2 + 2 signální slova
        prototype(far, unit(0.0, 1.0)),  # podobnost 0
    ]
    found = {p.narrative.code: (s, cues) for p, s, cues in
             labeling.find_candidates(document, protos, CENTER)}  # fmt: skip
    assert set(found) == {"a", "b"}
    assert found["b"][1] == ["pomoc ukrajin", "nevypla"]

    document.is_relevant = False
    assert labeling.find_candidates(document, protos, CENTER) == []


def test_queue_is_idempotent_and_text_change_resets():
    n = narrative()
    document = doc()
    protos = [prototype(n, unit(1.0))]
    assert labeling.queue_candidates(document, protos, CENTER) == 1
    assert labeling.queue_candidates(document, protos, CENTER) == 0

    decision = LabelDecision.objects.get()
    decision.present, decision.stance, decision.confidence = True, "pro", 0.9
    decision.save()
    labeling._apply(decision)
    assert Label.objects.count() == 1

    document.text = "Nový text: pomoc Ukrajině se nevyplácí."
    document.save()  # nový hash textu
    assert labeling.queue_candidates(document, protos, CENTER) == 1
    assert Label.objects.count() == 0  # štítek ze starého textu se přepočítá
    assert LabelDecision.objects.count() == 2  # staré rozhodnutí zůstává jako historie


def test_decide_document_stores_all_decisions_and_labels_only_confident(monkeypatch):
    a, b, c = narrative("a"), narrative("b"), narrative("c")
    document = doc()
    labeling.queue_candidates(document, [prototype(x, unit(1.0)) for x in (a, b, c)], CENTER)
    seen = {}

    def fake(model, system, user, schema, client=None):
        seen.update(model=model, user=user, schema=schema)
        return completion(answer("a"), answer("b", present=False, stance="contra"),
                          answer("c", stance="contra", confidence=0.3), cost=0.003)  # fmt: skip

    monkeypatch.setattr(llm, "complete_json", fake)
    with KEY:
        stats = labeling.label_pending()
    assert (stats.documents, stats.decisions, stats.labels, stats.errors) == (1, 3, 1, 0)
    assert seen["model"] == "anthropic/claude-haiku-5.5"
    assert "### a" in seen["user"] and "Postoj proti: Vyvrací ho." in seen["user"]
    assert seen["schema"]["properties"]["decisions"]["items"]["properties"]["narrative"][
        "enum"
    ] == ["a", "b", "c"]

    decisions = {d.narrative.code: d for d in LabelDecision.objects.all()}
    assert decisions["b"].present is False and decisions["b"].stance == "neutral"
    assert decisions["c"].present and decisions["c"].confidence == 0.3  # k ruční kontrole
    assert decisions["a"].cost_usd == pytest.approx(0.001)
    label = Label.objects.get()
    assert (label.narrative.code, label.stance, label.origin) == ("a", "pro", "model")
    assert label.details["carrier"] == "autor" and label.details["evidence"]
    assert labeling.pending().count() == 0


def test_invalid_model_answer_is_an_error_not_a_label(monkeypatch):
    n = narrative()
    labeling.queue_candidates(doc(), [prototype(n, unit(1.0))], CENTER)
    bad = dict(answer(n.code), present="ano")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: completion(bad))
    with KEY:
        labeling.label_pending()
    decision = LabelDecision.objects.get()
    assert decision.present is None and "present" in decision.error
    assert not Label.objects.exists()


def test_label_pending_without_key_or_over_budget(monkeypatch):
    n = narrative()
    labeling.queue_candidates(doc(), [prototype(n, unit(1.0))], CENTER)
    with override_settings(OPENROUTER_API_KEY=""):
        assert labeling.label_pending().stopped == "chybí OPENROUTER_API_KEY"

    monkeypatch.setattr(labeling, "month_spend", lambda today=None: 99.0)
    with KEY:
        stats = labeling.label_pending()
        labeling.label_pending()
    assert stats.stopped.startswith("vyčerpán měsíční rozpočet")
    assert Notification.objects.filter(title__contains="rozpočet").count() == 1  # jednou
    assert labeling.pending().count() == 1


def test_llm_error_is_recorded_and_retry_requeues(monkeypatch):
    n = narrative()
    labeling.queue_candidates(doc(), [prototype(n, unit(1.0))], CENTER)

    def broken(*args, **kwargs):
        raise llm.LLMError("HTTP 400: chyba")

    monkeypatch.setattr(llm, "complete_json", broken)
    with KEY:
        assert labeling.label_pending().errors == 1
    assert LabelDecision.objects.get().error == "HTTP 400: chyba"
    assert labeling.pending().count() == 0
    assert labeling.retry_errors() == 1 and labeling.pending().count() == 1


def test_openrouter_request_and_retry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, text="rate limit")
        body = {"model": "x/y", "usage": {"cost": 0.01, "prompt_tokens": 5},
                "choices": [{"message": {"content": '{"decisions": []}'}}]}  # fmt: skip
        return httpx.Response(200, json=body)

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with KEY, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = llm.complete_json("x/y", "sys", "user", {"type": "object"}, client=client)
    assert result.data == {"decisions": []} and result.cost_usd == 0.01
    assert calls[0]["provider"] == {"zdr": True, "data_collection": "deny"}  # bez uchování dat
    assert calls[0]["response_format"]["type"] == "json_schema"

    denied = httpx.MockTransport(lambda r: httpx.Response(401))
    with KEY, httpx.Client(transport=denied) as c, pytest.raises(llm.LLMError, match="HTTP 401"):
        llm.complete_json("x/y", "sys", "user", {}, client=c)


def test_long_text_excerpt_keeps_cue_windows():
    n = narrative(cues=("korespondencn",))
    text = "Úvod. " * 1500 + "Korespondenční volba je podvod." + " Konec." * 500
    document = doc(text=text)
    excerpt = labeling._excerpt(document, [prototype(n, unit(1.0))])
    assert len(excerpt) < len(text) and "Korespondenční volba je podvod." in excerpt


def test_new_narrative_version_starts_relabel_job(monkeypatch, django_capture_on_commit_callbacks):
    n = narrative()
    monkeypatch.setattr(labeling, "queue_narrative", lambda narrative: 7)
    with django_capture_on_commit_callbacks(execute=True):
        n.definition = "Upravená definice."
        n.save(change_note="Zpřesnění")
    job = Job.objects.get(job_type=Job.Type.RELABEL)
    assert job.status == Job.Status.SUCCEEDED and job.result["queued"] == 7
    assert job.params == {"narrative": n.code, "version": 2}
    assert Notification.objects.filter(title__contains="Zpětné anotování").exists()


def test_current_labels_follow_latest_version():
    n = narrative()
    document = doc()
    Label.objects.create(document=document, narrative=n, narrative_version=n.latest_version,
                         stance="pro", origin="model", confidence=0.9)  # fmt: skip
    assert Label.objects.current().count() == 1
    n.definition = "Nová definice."
    n.save()
    assert Label.objects.current().count() == 0 and Label.objects.count() == 1


def test_import_codebook_file_is_valid():
    call_command("import_topics", stdout=io.StringIO())
    out = io.StringIO()
    call_command("import_codebook", stdout=out)
    assert "nové 5" in out.getvalue()
    call_command("import_codebook", "--update", stdout=out)
    assert "aktualizované 0" in out.getvalue()
    sssr = Narrative.objects.get(code="role-sssr-osvobozeni")
    assert sssr.kind == "theme" and sssr.cues and sssr.stance_guide["contra"]
    assert "nepřipisuje" in Narrative.objects.get(code="korespondencni-volba").attribution
    assert Topic.objects.filter(code__in=["bezpecnost", "volby", "system", "historie"]).count() == 4


def test_command_without_key_reports_waiting():
    n = narrative()
    labeling.queue_candidates(doc(), [prototype(n, unit(1.0))], CENTER)
    out = io.StringIO()
    with override_settings(OPENROUTER_API_KEY=""):
        call_command("label_narratives", stdout=out)
    assert "Kandidátů čeká 1" in out.getvalue() and "OPENROUTER_API_KEY" in out.getvalue()


def test_month_spend_counts_current_month():
    n = narrative()
    document = doc()
    labeling.queue_candidates(document, [prototype(n, unit(1.0))], CENTER)
    LabelDecision.objects.update(
        cost_usd=0.5, decided_at=datetime.datetime(2026, 11, 2, tzinfo=datetime.UTC)
    )
    assert labeling.month_spend(datetime.date(2026, 11, 20)) == 0.5
    assert labeling.month_spend(datetime.date(2026, 12, 1)) == 0
