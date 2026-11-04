"""Kandidáti na narativ v panelu: fragmenty textu a rozhodnutí tlačítkem."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.actors.models import Layer
from apps.narratives.models import (
    NarrativeCandidate,
    NarrativeStatus,
    NovelClaim,
    Screening,
)
from apps.panel.candidates import fragment, fragments
from apps.panel.models import AuditLog

from .factories import DocumentFactory, NarrativeFactory

pytestmark = pytest.mark.django_db

TEXT = (
    "Ve čtvrtek se konalo jednání vlády. Ministr řekl novinářům: „Ukrajina za útok na "
    "minibus může sama, je to provokace.“ Podle expertů to nemá oporu v důkazech. "
    "Opozice žádá vysvětlení."
)


def make_candidate(n_docs=3, evidence="Ukrajina za útok na minibus může sama", **claim_kwargs):
    today = timezone.localdate()
    candidate = NarrativeCandidate.objects.create(
        description="Ukrajina si útok na minibus způsobila sama.",
        window_start=today - datetime.timedelta(days=7), window_end=today,
        n_documents=n_docs, n_authors=n_docs, layers=[Layer.MAINSTREAM],
    )  # fmt: skip
    for _ in range(n_docs):
        document = DocumentFactory(text=TEXT)
        screening = Screening.objects.create(document=document, text_hash=document.text_hash)
        NovelClaim.objects.create(
            screening=screening, document=document, candidate=candidate,
            claim=candidate.description, stance="pro", confidence=0.8, evidence=evidence,
            **{"carrier": "ministr", **claim_kwargs},
        )  # fmt: skip
    return candidate


def test_fragment_highlights_evidence_in_context():
    candidate = make_candidate(n_docs=1)
    f = fragment(candidate.claims.get())
    assert f.found and f.match == "Ukrajina za útok na minibus může sama"
    assert f.before.endswith("novinářům: „") and f.after.startswith(", je to provokace")
    assert f.in_quotes and not f.by_author and f.is_quote


def test_fragment_by_author_without_quotes():
    candidate = make_candidate(n_docs=1, evidence="Podle expertů to nemá oporu", carrier="autor")
    f = fragment(candidate.claims.get())
    assert f.found and not f.in_quotes and f.by_author and not f.is_quote


def test_fuzzy_evidence_and_missing_evidence():
    candidate = make_candidate(n_docs=1, evidence="Podle expertu to nema oporu v dukazech.")
    assert fragment(candidate.claims.get()).match.startswith("Podle expertů")
    claim = candidate.claims.get()
    claim.evidence = "Něco, co v textu vůbec není a nikdy nebylo napsáno."
    assert not fragment(claim).found


def test_fragments_limit_and_distinct_documents():
    candidate = make_candidate(n_docs=5)
    chosen = fragments(candidate)
    assert len(chosen) == 3 and len({f.claim.document_id for f in chosen}) == 3


def test_detail_and_list_pages(researcher_client):
    candidate = make_candidate()
    candidate.nearest_narratives.add(NarrativeFactory(name="Blízký narativ"))
    page = researcher_client.get(reverse("panel:candidate", args=[candidate.pk])).content.decode()
    assert "<mark>Ukrajina za útok na minibus může sama</mark>" in page
    assert "citace: ministr" in page and "Blízký narativ" in page
    assert "Přijmout do narativů" in page and "Zamítnout" in page
    listing = researcher_client.get(reverse("panel:candidates")).content.decode()
    assert "Ukrajina si útok na minibus způsobila sama." in listing


def test_reject_goes_to_next_and_is_audited(researcher_client):
    first, second = make_candidate(n_docs=3), make_candidate(n_docs=2)
    response = researcher_client.post(
        reverse("panel:candidate_decide", args=[first.pk]),
        {"decision": "reject", "reason": "Jen citace ministra."},
    )
    assert response.url == reverse("panel:candidate", args=[second.pk])
    first.refresh_from_db()
    assert first.status == NarrativeCandidate.Status.REJECTED
    assert first.decision_reason == "Jen citace ministra."
    entry = AuditLog.objects.get(action=AuditLog.Action.DECISION)
    assert entry.changes == {"status": ["Nový", "Zamítnut"]} and entry.note
    # повторное решение не проходит
    researcher_client.post(
        reverse("panel:candidate_decide", args=[first.pk]), {"decision": "accept"}
    )
    first.refresh_from_db()
    assert first.status == NarrativeCandidate.Status.REJECTED


def test_accept_activates_tracked_narrative(researcher_client):
    candidate = make_candidate()
    narrative = NarrativeFactory(status=NarrativeStatus.CANDIDATE, discovered=True)
    candidate.resulting_narrative, candidate.tracked_since = narrative, timezone.now()
    candidate.save()
    response = researcher_client.post(
        reverse("panel:candidate_decide", args=[candidate.pk]), {"decision": "accept"}
    )
    assert response.url == reverse("panel:candidates")
    narrative.refresh_from_db()
    assert narrative.status == NarrativeStatus.ACTIVE


def test_observer_has_no_access(observer_client):
    candidate = make_candidate(n_docs=1)
    assert observer_client.get(reverse("panel:candidate", args=[candidate.pk])).status_code == 403
