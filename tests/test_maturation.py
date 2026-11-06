"""Дозревание кандидатов: малые кластеры без пополнения уходят к существующим узлам
или истекают; видимые получают только предложение; истёкший кластер оживает."""

import datetime
from io import StringIO

import numpy as np
import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.narratives.models import (
    LabelDecision,
    NarrativeCandidate,
    NarrativeStatus,
    NovelClaim,
    Screening,
)
from apps.panel.models import Notification
from apps.processing import llm, novelty

from .factories import DocumentFactory, NarrativeFactory
from .test_novelty import VECTORS, claim, rule, space  # noqa: F401

pytestmark = pytest.mark.django_db

Status = NarrativeCandidate.Status
TODAY = datetime.date(2026, 11, 6)


def cluster(n_docs=1, age=20, text="Raketu na nemocnici vypálila Ukrajina.", vector=None):
    day = TODAY - datetime.timedelta(days=age)
    vector = VECTORS[text] if vector is None else vector
    candidate = NarrativeCandidate.objects.create(
        description=text, centroid=list(vector), window_start=day, window_end=day,
        n_documents=n_docs, n_authors=n_docs,
    )  # fmt: skip
    for _ in range(n_docs):
        document = DocumentFactory()
        screening = Screening.objects.create(document=document, text_hash=document.text_hash)
        NovelClaim.objects.create(
            screening=screening, document=document, candidate=candidate, claim=text,
            stance="pro", confidence=0.8, embedding=list(vector),
        )  # fmt: skip
    return candidate


@pytest.fixture
def answer(monkeypatch):
    """Ответ модели: узел (или None) для каждого кандидата."""
    calls = []
    choice = {"target": None}

    def fake(candidate, center=None):
        calls.append(candidate.pk)
        target = choice["target"]
        return target, "Stejné tvrzení." if target else "Nic nesedí.", 0.001

    monkeypatch.setattr(novelty, "match_existing", fake)
    monkeypatch.setattr(novelty, "get_center", lambda model=None: np.zeros(768))
    choice["calls"] = calls
    return choice


def test_stalled_selects_only_old_open_untracked():
    old, fresh = cluster(age=20), cluster(age=3)
    decided = cluster(age=20)
    decided.status = Status.REJECTED
    decided.save()
    tracked = cluster(age=20)
    tracked.tracked_since = timezone.now()
    tracked.save()
    assert list(novelty.stalled(TODAY, 14)) == [old]
    assert set(novelty.stalled(TODAY, 0)) == {old, fresh}


def test_small_cluster_distributed_to_node(answer):
    target = NarrativeFactory()
    candidate = cluster(n_docs=2)
    answer["target"] = target
    stats = novelty.mature(today=TODAY, days=14)
    assert stats["distributed"] == 1 and stats["targets"] == {target.code: 1}
    candidate.refresh_from_db()
    assert candidate.status == Status.DISTRIBUTED and candidate.resulting_narrative == target
    assert candidate.decision_reason.startswith("Automaticky po 14 dnech")
    decisions = LabelDecision.objects.filter(narrative=target)
    assert decisions.count() == 2 and all(d.present is None for d in decisions)


def test_small_cluster_without_node_expires(answer):
    candidate = cluster()
    stats = novelty.mature(today=TODAY, days=14)
    assert stats["expired"] == 1
    candidate.refresh_from_db()
    assert candidate.status == Status.EXPIRED and candidate.decided_at
    assert "Nic nesedí." in candidate.decision_reason
    assert not LabelDecision.objects.exists()


def test_visible_cluster_gets_only_suggestion(answer):
    target = NarrativeFactory()
    candidate = cluster(n_docs=3)
    answer["target"] = target
    stats = novelty.mature(today=TODAY, days=14)
    assert stats["suggested"] == 1 and stats["distributed"] == 0
    candidate.refresh_from_db()
    assert candidate.status == Status.NEW and candidate.suggested_narrative == target
    assert not LabelDecision.objects.exists()
    # без нового документа повторно модель не спрашиваем
    novelty.mature(today=TODAY, days=14)
    assert answer["calls"] == [candidate.pk]


def test_dry_run_changes_nothing(answer):
    candidate = cluster()
    stats = novelty.mature(today=TODAY, days=14, dry_run=True)
    assert stats["expired"] == 1
    candidate.refresh_from_db()
    assert candidate.status == Status.NEW


def test_llm_error_stops_batch(monkeypatch):
    cluster(), cluster()
    monkeypatch.setattr(novelty, "get_center", lambda model=None: np.zeros(768))

    def fail(candidate, center=None):
        raise llm.LLMError("402")

    monkeypatch.setattr(novelty, "match_existing", fail)
    stats = novelty.mature(today=TODAY, days=14)
    assert stats["error"] == "402" and stats["expired"] == 0
    assert NarrativeCandidate.objects.filter(status=Status.NEW).count() == 2


def test_bad_answer_skips_only_that_cluster(monkeypatch):
    first, second = cluster(age=30), cluster(age=20)
    monkeypatch.setattr(novelty, "get_center", lambda model=None: np.zeros(768))

    def flaky(candidate, center=None):
        if candidate == first:
            raise llm.LLMError("Neplatná odpověď modelu: prázdná")
        return None, "Nic nesedí.", 0.001

    monkeypatch.setattr(novelty, "match_existing", flaky)
    stats = novelty.mature(today=TODAY, days=14)
    assert stats["skipped"] == 1 and stats["expired"] == 1 and "error" not in stats
    first.refresh_from_db(), second.refresh_from_db()
    assert first.status == Status.NEW and second.status == Status.EXPIRED


def test_notification_summarizes_run(answer):
    cluster()
    novelty.notify_maturation(novelty.mature(today=TODAY, days=14))
    notification = Notification.objects.get()
    assert "Vypršelo: 1" in notification.text and "stav=dozrani" in notification.url
    novelty.notify_maturation(novelty.mature(today=TODAY, days=14))  # nic nového
    assert Notification.objects.count() == 1


def test_expired_cluster_revives_on_similar_claim(space):  # noqa: F811
    rule(99, 99)
    candidate = cluster(age=1)
    candidate.status, candidate.decided_at = Status.EXPIRED, timezone.now()
    candidate.save()
    document = DocumentFactory()
    screening = Screening.objects.create(document=document, text_hash=document.text_hash)
    novelty.record_claims(screening, [claim("Nemocnici zasáhla ukrajinská raketa.")])
    candidate.refresh_from_db()
    assert candidate.status == Status.NEW and candidate.decided_at is None
    assert candidate.claims.count() == 2 and "Znovu otevřen" in candidate.decision_reason


def test_match_existing_offers_nearest_nodes(monkeypatch, space):  # noqa: F811
    near = NarrativeFactory(name="Ukrajina ostřeluje civilisty")
    far = NarrativeFactory(name="Volby jsou zmanipulované")
    space.append((near, near.latest_version, VECTORS["Nemocnici zasáhla ukrajinská raketa."]))
    space.append((far, far.latest_version, VECTORS["Korespondenční volba je podvod."]))
    seen = {}

    def complete(model, system, prompt, schema, **kwargs):
        seen["prompt"] = prompt
        return llm.Completion(data={"code": near.code, "reason": "Konkrétní případ."}, model="m",
                              cost_usd=0.0004, prompt_tokens=1, completion_tokens=1)  # fmt: skip

    monkeypatch.setattr(llm, "complete_json", complete)
    candidate = cluster()
    target, reason, cost = novelty.match_existing(candidate, np.zeros(768, dtype=np.float32))
    assert target == near and reason == "Konkrétní případ." and cost == 0.0004
    assert seen["prompt"].index(near.code) < seen["prompt"].index(far.code)


def test_panel_tabs_split_queue(researcher_client):
    visible, small = cluster(n_docs=3, age=1), cluster(n_docs=1, age=1)
    gone = cluster(age=1)
    gone.status = Status.EXPIRED
    gone.save()
    url = reverse("panel:candidates")
    rows = {
        tab: set(researcher_client.get(url, {"stav": tab}).context["candidates"])
        for tab in ("", "inkubator", "dozrani")
    }
    assert rows == {"": {visible}, "inkubator": {small}, "dozrani": {gone}}


def test_panel_merge_to_node_and_reopen(researcher_client):
    target = NarrativeFactory(status=NarrativeStatus.ACTIVE)
    candidate = cluster(n_docs=3, age=1)
    decide = reverse("panel:candidate_decide", args=[candidate.pk])
    researcher_client.post(decide, {"decision": "merge", "target": "neexistuje"})
    candidate.refresh_from_db()
    assert candidate.status == Status.NEW  # bez platného cíle se nic nestane
    researcher_client.post(decide, {"decision": "merge", "target": target.code})
    candidate.refresh_from_db()
    assert candidate.status == Status.MERGED and candidate.resulting_narrative == target
    assert LabelDecision.objects.filter(narrative=target).count() == 3

    gone = cluster(age=1)
    gone.status = Status.EXPIRED
    gone.save()
    researcher_client.post(
        reverse("panel:candidate_decide", args=[gone.pk]), {"decision": "reopen"}
    )
    gone.refresh_from_db()
    assert gone.status == Status.NEW and gone.decided_at is None


def test_command_dry_run(answer):
    candidate = cluster()
    out = StringIO()
    call_command("mature_candidates", "--dry-run", "--days", "0", stdout=out)
    assert (
        "Zkouška (nic nezměněno): přiřazeno 0, vypršelo 1, návrhů 0, přeskočeno 0" in out.getvalue()
    )
    candidate.refresh_from_db()
    assert candidate.status == Status.NEW and not Notification.objects.exists()
