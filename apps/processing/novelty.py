"""Новые нарративы из потока документов (README §3, индуктивная часть).

1. При проверке документа (labeling.decide_document) модель перечисляет конкретные
   утверждения, которых нет среди известных нарративов (NovelClaim).
2. Утверждение сравнивается по эмбеддингу (центрированная близость):
   - с названием и каждым примером отслеживаемых нарративов (короткие фразы сравниваются
     с короткими: прототип «название + определение» к фразе не ближе 0,5, а отдельные
     формулировки дают 0,65–0,72 на настоящих совпадениях и ≤ 0,57 на прочих, калибровка
     4.11.2026): близкое (≥ `discovery-known-similarity`) — не новое,
     документ ставится в очередь на решение по этому нарративу;
   - с кластерами-кандидатами: близкое (≥ `discovery-cluster-similarity`) добавляется в
     ближайший, иначе начинает новый (онлайн-кластеризация: прогон архива не нужен).
3. Кластер, выполнивший правило добавления (N документов от K независимых авторов за окно W),
   начинает отслеживаться: модель составляет карточку нарратива (название, определение,
   указания к позициям, сигнальные слова), создаётся Narrative со статусом «Kandidát»,
   а документы кластера получают метки — это его история (первая фиксация, слои).
4. В анализ нарратив попадает, только когда исследователь его примет (decide).
"""

import logging

import numpy as np
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.text import slugify

from apps.documents.models import Document
from apps.narratives.models import (
    LabelDecision,
    Narrative,
    NarrativeCandidate,
    NarrativeStatus,
    NovelClaim,
    Screening,
    Stance,
    Topic,
)
from apps.panel.defaults import get_setting

from . import llm
from .embeddings import centered, embed, get_center, model_name

logger = logging.getLogger(__name__)

MAX_CLAIMS = 3
EXAMPLE_DOCUMENTS = 5
NEAREST_NARRATIVES = 3


def _clean(item) -> dict | None:
    """Ответ модели проверяется сам: не все провайдеры держат строгую схему."""
    if not isinstance(item, dict):
        return None
    claim = str(item.get("claim") or "").strip()
    if not claim or item.get("stance") not in Stance.values:
        return None
    try:
        confidence = max(0.0, min(1.0, float(item.get("confidence"))))
    except (TypeError, ValueError):
        return None
    return {
        "claim": claim[:1000],
        "stance": item["stance"],
        "confidence": confidence,
        "carrier": str(item.get("carrier") or "")[:200],
        "evidence": str(item.get("evidence") or "")[:1000],
    }


def record_claims(screening: Screening, items) -> int:
    """Утверждения из ответа модели → NovelClaim, распределённые по кластерам."""
    minimum = float(get_setting("labeling-min-confidence"))
    claims = [c for c in map(_clean, items or []) if c and c["confidence"] >= minimum]
    claims = claims[:MAX_CLAIMS]
    if not claims:
        return 0
    vectors = embed([c["claim"] for c in claims], model_name())
    center = get_center()
    for data, vector in zip(claims, vectors, strict=True):
        claim = NovelClaim.objects.create(
            screening=screening, document=screening.document, embedding=list(vector), **data
        )
        assign(claim, np.asarray(vector), center)
    return len(claims)


def _tracked_vectors():
    """[(нарратив, версия, вектор)] — по вектору на название и на каждый пример."""
    from .labeling import TRACKED
    from .relevance import _embed_cached

    rows = []
    for narrative in Narrative.objects.filter(status__in=TRACKED):
        version = narrative.latest_version
        rows += [(narrative, version, text) for text in (narrative.name, *narrative.examples)]
    if not rows:
        return []
    vectors = _embed_cached([str(text) for _, _, text in rows], model_name())
    return [(n, v, vector) for (n, v, _), vector in zip(rows, vectors, strict=True)]


def assign(claim: NovelClaim, vector: np.ndarray, center) -> None:
    """Утверждение → известный нарратив, существующий кластер или новый кластер."""
    if center is None:
        return
    target = centered(vector, center)[0]
    tracked = _tracked_vectors()
    if tracked:
        sims = [float(centered(v, center)[0] @ target) for _, _, v in tracked]
        best = int(np.argmax(sims))
        if sims[best] >= float(get_setting("discovery-known-similarity")):
            narrative, version, _ = tracked[best]
            claim.matched_narrative, claim.similarity = narrative, round(sims[best], 4)
            claim.save(update_fields=["matched_narrative", "similarity"])
            document = claim.document
            LabelDecision.objects.get_or_create(
                document=document,
                narrative_version=version,
                text_hash=document.text_hash,
                defaults={"narrative": narrative, "similarity": round(sims[best], 4)},
            )
            return
    open_candidates = list(
        NarrativeCandidate.objects.filter(
            status__in=[NarrativeCandidate.Status.NEW, NarrativeCandidate.Status.DEFERRED],
            tracked_since__isnull=True,
            centroid__isnull=False,
        )
    )
    candidate, similarity = None, None
    if open_candidates:
        sims = [
            float(centered(np.asarray(c.centroid), center)[0] @ target) for c in open_candidates
        ]
        best = int(np.argmax(sims))
        if sims[best] >= float(get_setting("discovery-cluster-similarity")):
            candidate, similarity = open_candidates[best], round(sims[best], 4)
    day = _day(claim.document)
    if candidate is None:
        candidate = NarrativeCandidate.objects.create(
            description=claim.claim, centroid=list(vector), window_start=day, window_end=day
        )
        similarity = 1.0
    claim.candidate, claim.similarity = candidate, similarity
    claim.save(update_fields=["candidate", "similarity"])
    refresh(candidate, center)


def _day(document: Document):
    moment = document.published_at or document.collected_at or timezone.now()
    return timezone.localdate(moment)


def _author(document: Document) -> str:
    return f"a{document.actor_id}" if document.actor_id else f"s{document.source_id}"


def refresh(candidate: NarrativeCandidate, center=None) -> bool:
    """Пересчёт карточки кластера; возвращает True, если кластер начал отслеживаться."""
    claims = list(candidate.claims.select_related("document__source"))
    if not claims:
        return False
    vectors = np.stack([np.asarray(c.embedding) for c in claims])
    candidate.centroid = list(vectors.mean(axis=0))
    window = int(get_setting("narrative-rule-window-days"))
    documents = {c.document_id: c.document for c in claims}
    end = max(_day(d) for d in documents.values())
    in_window = [d for d in documents.values() if (end - _day(d)).days < window]
    candidate.window_end = end
    candidate.window_start = min(_day(d) for d in in_window)
    candidate.n_documents = len(in_window)
    candidate.n_authors = len({_author(d) for d in in_window})
    candidate.layers = sorted({d.source.layer for d in documents.values() if d.source_id})
    candidate.save()
    candidate.example_documents.set(sorted(documents)[:EXAMPLE_DOCUMENTS])
    center = get_center() if center is None else center
    if center is not None:
        tracked = _tracked_vectors()
        target = centered(np.asarray(candidate.centroid), center)[0]
        ranked = sorted(tracked, key=lambda t: -float(centered(t[2], center)[0] @ target))
        nearest = list(dict.fromkeys(n for n, _, _ in ranked))[:NEAREST_NARRATIVES]
        candidate.nearest_narratives.set(nearest)
    if candidate.tracked_since is None and passes_rule(candidate):
        promote(candidate)
        return True
    return False


def recluster() -> int:
    """Пересборка неотслеживаемых кластеров заново (после смены порогов): утверждения
    распределяются в исходном порядке. Решённые и отслеживаемые кластеры не трогаются."""
    keep = Q(tracked_since__isnull=False) | ~Q(
        status__in=[NarrativeCandidate.Status.NEW, NarrativeCandidate.Status.DEFERRED]
    )
    claims = NovelClaim.objects.filter(matched_narrative__isnull=True).exclude(
        candidate__in=NarrativeCandidate.objects.filter(keep)
    )
    with transaction.atomic():
        ids = list(claims.order_by("pk").values_list("pk", flat=True))
        NarrativeCandidate.objects.exclude(keep).delete()
        center = get_center()
        for claim in NovelClaim.objects.filter(pk__in=ids).order_by("pk"):
            assign(claim, np.asarray(claim.embedding), center)
    return NarrativeCandidate.objects.exclude(keep).count()


def passes_rule(candidate: NarrativeCandidate) -> bool:
    return candidate.n_documents >= int(
        get_setting("narrative-rule-min-posts")
    ) and candidate.n_authors >= int(get_setting("narrative-rule-min-authors"))


# --- Карточка нарратива ----------------------------------------------------------

CARD_SYSTEM = """You help maintain the codebook of an academic research project (Jan \
Evangelista Purkyně University) that measures how narratives attributed to Russian influence \
operations spread in Czech media, Telegram and parliament.

You get claims extracted from several documents that express (roughly) the same new claim. \
Write a STRICT codebook entry for this single claim, IN CZECH:
- name: the claim in one short sentence (max 150 characters);
- definition: what exactly the claim says and what is not part of it (2–4 sentences);
- pro / contra / neutral / absent: when a document pushes the claim, explicitly refutes it, \
reports someone making it, and which topically similar texts do NOT contain it (an opposite \
general opinion on the topic without reacting to the claim is "absent");
- examples: up to 3 typical wordings pushing the claim;
- counter_examples: up to 2 wordings explicitly refuting it;
- cues: up to 8 Czech word stems without diacritics, lower case (e.g. "nemocnic", "raket");
- topic: the code of the best matching topic from the list."""


def _card_schema(topics: list[str]) -> dict:
    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    properties = {
        "name": text, "definition": text, "pro": text, "contra": text, "neutral": text,
        "absent": text, "examples": texts, "counter_examples": texts, "cues": texts,
        "topic": {"type": "string", "enum": topics},
    }  # fmt: skip
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _card_prompt(candidate: NarrativeCandidate, topics) -> str:
    claims = candidate.claims.select_related("document")[:20]
    lines = [f"- {c.claim} (citace: „{c.evidence[:200]}“)" for c in claims]
    topic_lines = [f"- {t.code}: {t.name}" for t in topics]
    return "## Topics\n" + "\n".join(topic_lines) + "\n\n## Claims\n" + "\n".join(lines)


def _code(candidate: NarrativeCandidate, name: str) -> str:
    return f"k{candidate.pk}-{slugify(name)}"[:50].rstrip("-")


def promote(candidate: NarrativeCandidate) -> Narrative | None:
    """Кластер выполнил правило → нарратив «Kandidát», отслеживается в новых документах."""
    from apps.panel.models import Notification

    topics = list(Topic.objects.order_by("code"))
    if not topics:
        return None
    card = {}
    if llm.is_configured():
        try:
            completion = llm.complete_json(
                str(get_setting("labeling-model")),
                CARD_SYSTEM,
                _card_prompt(candidate, topics),
                _card_schema([t.code for t in topics]),
            )
            card = completion.data if isinstance(completion.data, dict) else {}
        except llm.LLMError as exc:
            logger.warning("Karta narativu pro shluk %s se nepodařila: %s", candidate.pk, exc)
    name = str(card.get("name") or candidate.description)[:300]
    topic = next((t for t in topics if t.code == card.get("topic")), None) or _topic_of(
        candidate, topics
    )
    with transaction.atomic():
        narrative = Narrative(
            code=_code(candidate, name),
            topic=topic,
            name=name,
            definition=str(card.get("definition") or candidate.description),
            examples=[str(x) for x in card.get("examples") or []][:3],
            counter_examples=[str(x) for x in card.get("counter_examples") or []][:2],
            stance_guide={
                key: str(card[key])
                for key in ("pro", "contra", "neutral", "absent")
                if card.get(key)
            },  # fmt: skip
            cues=[str(x).strip().lower() for x in card.get("cues") or [] if str(x).strip()][:8],
            attribution="Navržen automaticky ze shluku nových tvrzení v dokumentech; "
            "atribuci doplní výzkumník.",
            status=NarrativeStatus.CANDIDATE,
            discovered=True,
        )
        narrative.save(change_note=f"Automaticky navržen ze shluku č. {candidate.pk}")
        candidate.resulting_narrative = narrative
        candidate.tracked_since = timezone.now()
        candidate.keywords = narrative.cues
        candidate.save()
        _label_members(candidate, narrative)
        Notification.objects.create(
            notification_type=Notification.Type.NARRATIVE_CANDIDATE,
            title=f"Nový kandidát na narativ: {name}"[:300],
            text=f"Shluk splnil pravidlo přidání ({candidate.n_documents} dokumentů, "
            f"{candidate.n_authors} autorů). Narativ se od teď sleduje v nových dokumentech; "
            "do analýzy patří až po přijetí.",
            url=f"/admin/narratives/narrativecandidate/{candidate.pk}/change/",
        )
    return narrative


def _topic_of(candidate: NarrativeCandidate, topics) -> Topic:
    codes = [(d.relevance_details or {}).get("topic") for d in candidate.example_documents.all()]
    for code, _ in sorted(((c, codes.count(c)) for c in set(codes) if c), key=lambda x: -x[1]):
        found = next((t for t in topics if t.code == code), None)
        if found:
            return found
    return topics[0]


def _label_members(candidate: NarrativeCandidate, narrative: Narrative) -> None:
    """Документы кластера — история нарратива: решения и метки без нового вызова модели."""
    from .labeling import _apply

    version = narrative.latest_version
    seen = set()
    for claim in candidate.claims.select_related("document", "screening").order_by("pk"):
        if claim.document_id in seen:
            continue
        seen.add(claim.document_id)
        decision, _ = LabelDecision.objects.update_or_create(
            document=claim.document,
            narrative_version=version,
            text_hash=claim.screening.text_hash,
            defaults={
                "narrative": narrative,
                "similarity": claim.similarity or 0,
                "model_name": claim.screening.model_name,
                "prompt_version": claim.screening.prompt_version,
                "present": True,
                "stance": claim.stance,
                "confidence": claim.confidence,
                "carrier": claim.carrier,
                "evidence": claim.evidence,
                "reason": f"Nové tvrzení ze shluku č. {candidate.pk}: {claim.claim}"[:1000],
                "decided_at": claim.created_at,
            },
        )
        _apply(decision)


# --- Решение исследователя -------------------------------------------------------


def decide(candidate: NarrativeCandidate, status: str, user=None, target=None) -> None:
    """Принять / объединить / отклонить / отложить кластер (README §9, страница кандидатов)."""
    Status = NarrativeCandidate.Status
    tracked = candidate.resulting_narrative if candidate.tracked_since else None
    candidate.status, candidate.decided_by, candidate.decided_at = status, user, timezone.now()
    with transaction.atomic():
        if status == Status.ACCEPTED:
            if tracked is None:
                tracked = promote(candidate)
            if tracked and tracked.status != NarrativeStatus.ACTIVE:
                tracked.status = NarrativeStatus.ACTIVE
                tracked.save(changed_by=user, change_note="Kandidát přijat výzkumníkem")
            candidate.resulting_narrative = tracked
        elif status == Status.MERGED:
            if target is None:
                raise ValueError("Sloučení potřebuje cílový narativ.")
            if tracked and tracked != target:
                tracked.status = NarrativeStatus.ARCHIVED
                tracked.save(changed_by=user, change_note=f"Sloučen do {target.code}")
            _queue_members(candidate, target)
            candidate.resulting_narrative = target
        elif status == Status.REJECTED and tracked:
            tracked.status = NarrativeStatus.ARCHIVED
            tracked.save(changed_by=user, change_note="Kandidát zamítnut výzkumníkem")
        candidate.save()


def _queue_members(candidate: NarrativeCandidate, target: Narrative) -> int:
    """Документы объединённого кластера → решение модели по целевому нарративу"""
    version, created = target.latest_version, 0
    documents = Document.objects.filter(novel_claims__candidate=candidate).distinct()
    for document in documents:
        _, new = LabelDecision.objects.get_or_create(
            document=document,
            narrative_version=version,
            text_hash=document.text_hash,
            defaults={"narrative": target, "similarity": 0},
        )
        created += new
    return created


def overview() -> dict:
    """Сводка по статусам для вывода команды."""
    return dict(NarrativeCandidate.objects.values_list("status").annotate(n=Count("id")).order_by())
