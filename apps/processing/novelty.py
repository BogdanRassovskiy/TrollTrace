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
5. Дозревание (mature, раз в сутки): кластер без нового документа `discovery-maturation-days`
   дней, не выполнивший правило, оценивает модель — к какому из ближайших узлов дерева
   относится утверждение (эмбеддингам здесь не доверяем: соседство по теме ≠ то же утверждение).
   - малый кластер (ниже порога видимости) — документы уходят на решение по найденному узлу
     («Přiřazen k narativu»), иначе кластер «Vypršel»; новое близкое утверждение его оживит;
   - видимый кластер — только предложение «patří k X», решает исследователь.
   Слои не фильтруются: утверждение может родиться и в парламенте или мейнстриме — интересно,
   подхватят ли его сети слоя A (решение 6.11.2026).
"""

import datetime
import json
import logging

import numpy as np
from django.db import transaction
from django.db.models import Count, F, Q
from django.urls import reverse
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
    Status = NarrativeCandidate.Status
    open_candidates = list(
        NarrativeCandidate.objects.filter(
            status__in=[Status.NEW, Status.DEFERRED, Status.EXPIRED],
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
    if candidate.status == Status.EXPIRED:  # утверждение повторилось — кластер снова в игре
        candidate.status, candidate.decided_at = Status.NEW, None
        candidate.decision_reason = "Znovu otevřen: přibylo podobné tvrzení."
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
operations spread in Czech media, Telegram and parliament (some source texts are Russian \
state media in Russian).

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
- topic: the code of the best matching topic from the list.
Every text field must be entirely in Czech — no English words or phrases such as "Claim \
asserts that" (start the definition e.g. with "Tvrzení říká, že…")."""


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
            url=reverse("panel:candidate", args=[candidate.pk]),
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
        elif status in (Status.MERGED, Status.DISTRIBUTED):
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


# --- Дозревание ------------------------------------------------------------------

MATCH_NODES = 6  # сколько ближайших узлов показать модели
MATURE_MAX_FAILURES = 3  # битых ответов подряд до остановки дозревания

MATCH_SYSTEM = """You maintain the codebook of an academic project tracking narratives in Czech \
media, Telegram and parliament. A cluster of documents contains a claim that did not match any \
codebook node strongly enough. Decide whether the claim IS one of the given codebook nodes.

A claim belongs to a node only if a text asserting the claim would be coded as asserting the \
node's claim (the same claim, or a narrower case / concrete instance of it). Same topic, same \
actors or shared vocabulary is NOT enough. Prefer the most specific fitting node \
(podnarativ > narativ > nadnarativ). If none fits, return code "".
Write the reason in Czech, one short sentence."""


def _match_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["code", "reason"],
        "properties": {"code": {"type": "string"}, "reason": {"type": "string"}},
    }


def nearest_nodes(candidate: NarrativeCandidate, center=None, k: int = MATCH_NODES) -> list:
    """Ближайшие отслеживаемые узлы по эмбеддингу (название и примеры) — кандидаты для модели."""
    center = get_center() if center is None else center
    if center is None or candidate.centroid is None:
        return []
    target = centered(np.asarray(candidate.centroid), center)[0]
    best: dict = {}
    for narrative, _, vector in _tracked_vectors():
        sim = float(centered(vector, center)[0] @ target)
        if sim > best.get(narrative.pk, (-1.0, None))[0]:
            best[narrative.pk] = (sim, narrative)
    ranked = sorted(best.values(), key=lambda x: -x[0])
    return [n for _, n in ranked[:k]]


def _match_prompt(candidate: NarrativeCandidate, nodes: list) -> str:
    claims = list(candidate.claims.order_by("pk").values_list("claim", flat=True)[:6])
    items = [
        {
            "code": n.code,
            "level": n.get_level_display(),
            "parent": n.parent.name if n.parent_id else "",
            "name": n.name,
            "definition": n.definition[:500],
        }
        for n in nodes
    ]
    return (
        "Cluster claims:\n- "
        + "\n- ".join(claims or [candidate.description])
        + "\n\nCodebook nodes:\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )


def match_existing(
    candidate: NarrativeCandidate, center=None
) -> tuple[Narrative | None, str, float]:
    """Модель решает, относится ли утверждение кластера к одному из ближайших узлов.
    Возвращает (узел или None, обоснование, цена)."""
    nodes = nearest_nodes(candidate, center)
    if not nodes:
        return None, "Žádný sledovaný narativ.", 0.0
    completion = llm.complete_json(
        str(get_setting("labeling-model")),
        MATCH_SYSTEM,
        _match_prompt(candidate, nodes),
        _match_schema(),
        max_tokens=600,
    )
    code = str(completion.data.get("code") or "")
    reason = str(completion.data.get("reason") or "")[:500]
    target = next((n for n in nodes if n.code == code), None)
    return target, reason, completion.cost_usd or 0.0


def is_visible(candidate: NarrativeCandidate) -> bool:
    return candidate.tracked_since is not None or (
        candidate.n_documents >= int(get_setting("discovery-visible-min-posts"))
        and candidate.n_authors >= int(get_setting("discovery-visible-min-authors"))
    )


def stalled(today=None, days: int | None = None):
    """Кластеры без нового документа `discovery-maturation-days` дней, ещё не решённые.
    Видимые, по которым предложение уже есть и с тех пор ничего не прибыло, — пропускаются."""
    today = today or timezone.localdate()
    days = int(get_setting("discovery-maturation-days")) if days is None else days
    border = today - datetime.timedelta(days=days)
    return (
        NarrativeCandidate.objects.filter(
            status=NarrativeCandidate.Status.NEW,
            tracked_since__isnull=True,
            window_end__lte=border,
        )
        .exclude(suggested_at__date__gt=F("window_end"))
        .order_by("window_end", "pk")
    )


def mature(*, today=None, days: int | None = None, limit: int | None = None,
           dry_run: bool = False) -> dict:  # fmt: skip
    """Дозревание: см. п. 5 в начале модуля. Возвращает счётчики и цену."""
    Status = NarrativeCandidate.Status
    limit = limit or int(get_setting("discovery-maturation-batch"))
    days = int(get_setting("discovery-maturation-days")) if days is None else days
    stats = {"distributed": 0, "expired": 0, "suggested": 0, "skipped": 0, "cost_usd": 0.0,
             "targets": {}}  # fmt: skip
    center, failures = get_center(), 0
    for candidate in stalled(today, days)[:limit]:
        try:
            target, reason, cost = match_existing(candidate, center)
        except llm.LLMError as exc:
            # битый ответ — кластер остаётся до следующего прогона; отказ API или серия
            # ошибок подряд — стоп (кредиты, недоступность)
            failures += 1
            stats["skipped"] += 1
            if failures >= MATURE_MAX_FAILURES or not str(exc).startswith("Neplatná odpověď"):
                logger.warning("Dozrávání: model neodpověděl (%s), zastaveno.", exc)
                stats["error"] = str(exc)
                break
            continue
        failures = 0
        stats["cost_usd"] += cost
        if is_visible(candidate):
            stats["suggested"] += 1
            if not dry_run:
                candidate.suggested_narrative, candidate.suggestion_reason = target, reason
                candidate.suggested_at = timezone.now()
                candidate.save(
                    update_fields=["suggested_narrative", "suggestion_reason", "suggested_at"]
                )
            continue
        prefix = (
            f"Automaticky po {days} dnech bez nového dokumentu: " if days
            else "Automaticky (ruční dozrání fronty): "
        )  # fmt: skip
        if target is not None:
            stats["distributed"] += 1
            stats["targets"][target.code] = stats["targets"].get(target.code, 0) + 1
            if not dry_run:
                candidate.decision_reason = prefix + reason
                decide(candidate, Status.DISTRIBUTED, target=target)
        else:
            stats["expired"] += 1
            if not dry_run:
                candidate.status, candidate.decided_at = Status.EXPIRED, timezone.now()
                candidate.decision_reason = prefix + (reason or "nepatří k žádnému narativu.")
                candidate.save(update_fields=["status", "decided_at", "decision_reason"])
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


def notify_maturation(stats: dict) -> None:
    """Сводка дозревания в панель (только когда что-то произошло)."""
    from apps.panel.models import Notification

    done = stats["distributed"] + stats["expired"] + stats["suggested"]
    if not done and not stats.get("error"):
        return
    targets = ", ".join(f"{code} ({n})" for code, n in sorted(
        stats["targets"].items(), key=lambda kv: -kv[1])[:8])  # fmt: skip
    text = (
        f"Přiřazeno k narativům: {stats['distributed']}"
        + (f" — {targets}" if targets else "")
        + f". Vypršelo: {stats['expired']}. Návrhů u viditelných kandidátů: {stats['suggested']}."
        + (f" Přeskočeno (neplatná odpověď): {stats['skipped']}." if stats["skipped"] else "")
        + f" Cena {stats['cost_usd']} USD."
        + (f" Přerušeno: {stats['error']}" if stats.get("error") else "")
    )
    Notification.objects.create(
        notification_type=Notification.Type.NARRATIVE_CANDIDATE,
        title="Dozrávání kandidátů na narativ",
        text=text[:2000],
        url=reverse("panel:candidates") + "?stav=dozrani",
    )


def overview() -> dict:
    """Сводка по статусам для вывода команды."""
    return dict(NarrativeCandidate.objects.values_list("status").annotate(n=Count("id")).order_by())
