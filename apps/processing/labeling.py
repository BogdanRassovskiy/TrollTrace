"""Разметка нарративов (README §3; §4, шаг 7): кандидаты → решение LLM → метки.

1. Кандидаты. Для релевантного документа считается центрированная близость к каждому
   активному нарративу (название, определение, примеры) и ищутся сигнальные слова.
   Кандидат — близость ≥ `labeling-candidate-similarity` или ≥ `labeling-cue-similarity`
   при двух и более сигнальных словах. Пороги мягкие: пропуск дороже лишнего вызова.
2. Решение. Один вызов LLM (OpenRouter, настройка `labeling-model`) на документ по всем его
   кандидатам: есть ли нарратив, позиция, уверенность, цитата-доказательство, кто нарратив
   несёт. Решения хранятся все, в том числе отрицательные (LabelDecision).
3. Метки. Положительное решение с уверенностью ≥ `labeling-min-confidence` → Label
   (origin = model) на версию нарратива. Ручные метки не трогаются.

Близость эмбеддингов ловит тему, а не позицию, а локальная NLI-модель не понимает чешский
подтекст (проверено на примерах) — поэтому решение за LLM, а эмбеддинги только сужают поток.
Новая версия нарратива (правка кодбука) → ретроразметка архива: кандидаты по новой версии
ставятся в очередь, метки прежних версий остаются историей (Label.objects.current()).
"""

import datetime
import logging
import time
from dataclasses import dataclass

import numpy as np
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.documents.models import Document
from apps.narratives.models import (
    Label,
    LabelDecision,
    Narrative,
    NarrativeKind,
    NarrativeStatus,
    NarrativeVersion,
    Stance,
)
from apps.panel.defaults import get_setting

from . import llm
from .embeddings import centered, get_center, model_name
from .relevance import _embed_cached, stem_pattern
from .text import fold

logger = logging.getLogger(__name__)

PROMPT_VERSION = "p2"  # p2: přísná přítomnost narativu (jen reakce na konkrétní tvrzení)
MIN_CUES = 2
HEAD_CHARS = 6000  # начало текста целиком
CUE_WINDOW = 400  # и окна вокруг сигнальных слов дальше по тексту
MAX_EXCERPT_CHARS = 9000


@dataclass
class Prototype:
    narrative: Narrative
    version: NarrativeVersion
    vector: np.ndarray  # нецентрированный
    patterns: list


def load_prototypes(narratives=None) -> list[Prototype]:
    narratives = narratives or list(
        Narrative.objects.filter(status=NarrativeStatus.ACTIVE).select_related("topic")
    )
    if not narratives:
        return []
    texts = [f"{n.name}. {n.definition} {' '.join(map(str, n.examples))}" for n in narratives]
    vectors = _embed_cached(texts, model_name())
    return [
        Prototype(
            narrative=n,
            version=n.latest_version,
            vector=vector,
            patterns=[(cue, stem_pattern(fold(cue).strip())) for cue in n.cues if cue.strip()],
        )
        for n, vector in zip(narratives, vectors, strict=True)
    ]


def _cues(prototype: Prototype, folded: str) -> list[str]:
    return [cue for cue, pattern in prototype.patterns if pattern.search(folded)]


def find_candidates(document: Document, prototypes: list[Prototype], center) -> list:
    """[(prototype, similarity, cues)] для релевантного документа с эмбеддингом."""
    if not document.is_relevant or document.embedding is None or center is None:
        return []
    high = float(get_setting("labeling-candidate-similarity"))
    low = float(get_setting("labeling-cue-similarity"))
    vector = centered(document.embedding, center)[0]
    folded = fold(f"{document.title}\n{document.text}")
    found = []
    for prototype in prototypes:
        similarity = round(float(centered(prototype.vector, center)[0] @ vector), 4)
        cues = _cues(prototype, folded)
        if similarity >= high or (similarity >= low and len(cues) >= MIN_CUES):
            found.append((prototype, similarity, cues))
    return found


def queue_candidates(document: Document, prototypes=None, center=None) -> int:
    """Кандидаты документа → ожидающие решения. Повтор безопасен; уже решённые пары
    (тот же текст и та же версия нарратива) повторно модели не отправляются."""
    prototypes = load_prototypes() if prototypes is None else prototypes
    center = get_center() if center is None else center
    created = 0
    for prototype, similarity, cues in find_candidates(document, prototypes, center):
        stale = LabelDecision.objects.filter(
            document=document, narrative_version=prototype.version
        ).exclude(text_hash=document.text_hash)
        if stale.exists():  # текст изменился: прежние решения — история, метка пересчитается
            stale.filter(present__isnull=True).delete()
            Label.objects.filter(
                document=document, narrative_version=prototype.version, origin=Label.Origin.MODEL
            ).delete()
        _, new = LabelDecision.objects.get_or_create(
            document=document,
            narrative_version=prototype.version,
            text_hash=document.text_hash,
            defaults={
                "narrative": prototype.narrative,
                "similarity": similarity,
                "cues": cues,
            },
        )
        created += new
    return created


# --- Решение LLM ---------------------------------------------------------------

SYSTEM = """You are a careful annotator in an academic research project (Jan Evangelista \
Purkyně University) that measures how narratives attributed to Russian influence operations \
spread in Czech media, Telegram channels and parliament. You annotate; you do not fact-check.

Narratives are defined STRICTLY. A narrative is a specific claim, not a topic; some are \
broad, some are very narrow (e.g. one claim about a single incident). Apply each definition \
literally.

For EVERY codebook narrative listed by the user decide:
- present: true ONLY if the document engages with this specific claim: it states or \
endorses it, explicitly refutes it or labels it propaganda/manipulation, or reports that \
someone makes it. Topical overlap is NOT enough. A document that merely holds the opposite \
general position on the topic without addressing the claim (e.g. supports aid to Ukraine \
without reacting to the claim that aid does not pay off) does NOT contain the narrative: \
present = false. The "Není narativ" line of each narrative lists such cases.
- stance (of the document as a whole, i.e. its author or outlet):
  "pro" = the document pushes or endorses the claim;
  "contra" = it explicitly refutes the claim or presents it as propaganda/manipulation;
  "neutral" = it reports that someone makes the claim, without taking a side.
  Use "neutral" when present is false.
- carrier: who voices the narrative in the text — "autor" if it is the author/outlet, \
otherwise the name of the quoted person or organisation; empty string if not present.
- evidence: a verbatim quote (max 300 characters) from the document supporting the decision; \
empty string if not present.
- confidence: 0–1, how sure you are about present AND stance together.
- reason: one short sentence IN CZECH explaining the decision.

Follow the stance guide of each narrative. Judge only the text given, not the source."""


def _excerpt(document: Document, prototypes: list[Prototype]) -> str:
    text = document.text or ""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    parts, used = [text[:HEAD_CHARS]], HEAD_CHARS
    folded = fold(text)  # fold zachovává délku pro češtinu (diakritika se jen odstraní)
    for prototype in prototypes:
        for _, pattern in prototype.patterns:
            for match in pattern.finditer(folded, HEAD_CHARS):
                if used >= MAX_EXCERPT_CHARS:
                    break
                start = max(HEAD_CHARS, match.start() - CUE_WINDOW // 2)
                parts.append(text[start : start + CUE_WINDOW])
                used += CUE_WINDOW
                break  # jedno okno na signální slovo
    return "\n[…]\n".join(parts)


def _narrative_block(narrative: Narrative) -> str:
    guide = narrative.stance_guide or {}
    lines = [
        f"### {narrative.code}",
        f"Název: {narrative.name}",
        "Druh: téma vlivu, ne zformulovaný narativ" if narrative.kind == NarrativeKind.THEME
        else "Druh: narativ",
        f"Definice: {narrative.definition}",
    ]  # fmt: skip
    labels = (("pro", "Postoj pro"), ("contra", "Postoj proti"), ("neutral", "Neutrálně"),
              ("absent", "Není narativ"))  # fmt: skip
    for key, label in labels:
        if guide.get(key):
            lines.append(f"{label}: {guide[key]}")
    if narrative.examples:
        lines.append("Příklady (pro): " + " | ".join(map(str, narrative.examples)))
    if narrative.counter_examples:
        lines.append("Příklady (proti): " + " | ".join(map(str, narrative.counter_examples)))
    return "\n".join(lines)


def build_prompt(document: Document, prototypes: list[Prototype]) -> str:
    published = document.published_at.date().isoformat() if document.published_at else "?"
    codebook = "\n\n".join(_narrative_block(p.narrative) for p in prototypes)
    return (
        f"## Codebook\n\n{codebook}\n\n## Document\n\nDatum: {published}\n"
        f"Titulek: {document.title}\n\n{_excerpt(document, prototypes)}"
    )


def schema(codes: list[str]) -> dict:
    item = {
        "type": "object",
        "properties": {
            "narrative": {"type": "string", "enum": codes},
            "present": {"type": "boolean"},
            "stance": {"type": "string", "enum": list(Stance.values)},
            "confidence": {"type": "number"},
            "carrier": {"type": "string"},
            "evidence": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["narrative", "present", "stance", "confidence", "carrier", "evidence",
                     "reason"],
        "additionalProperties": False,
    }  # fmt: skip
    return {
        "type": "object",
        "properties": {"decisions": {"type": "array", "items": item}},
        "required": ["decisions"],
        "additionalProperties": False,
    }


def decide_document(document: Document, decisions: list[LabelDecision], model: str,
                    client=None, *, save: bool = True):  # fmt: skip
    """Один вызов модели по всем ожидающим решениям документа.

    Возвращает (ответы модели по решениям, Completion). save=False — только сравнение моделей.
    """
    prototypes = [_prototype_of(d) for d in decisions]
    codes = [p.narrative.code for p in prototypes]
    completion = llm.complete_json(
        model, SYSTEM, build_prompt(document, prototypes), schema(codes), client=client
    )
    items = completion.data.get("decisions") if isinstance(completion.data, dict) else None
    answers = {a.get("narrative"): a for a in items or [] if isinstance(a, dict)}
    cost = completion.cost_usd / len(decisions) if completion.cost_usd is not None else None
    results = []
    for decision in decisions:
        answer = answers.get(decision.narrative.code)
        results.append(answer or {})
        if not save:
            continue
        decision.model_name, decision.prompt_version = model, PROMPT_VERSION
        decision.cost_usd, decision.decided_at = cost, timezone.now()
        if answer is None:
            decision.error = "Model o narativu nerozhodl."
            decision.save()
            continue
        problem = _invalid(answer)
        if problem:
            decision.error = f"Neplatná odpověď modelu: {problem}"
            decision.save()
            continue
        decision.present = answer["present"]
        decision.stance = answer["stance"] if decision.present else Stance.NEUTRAL
        decision.confidence = max(0.0, min(1.0, float(answer["confidence"])))
        decision.evidence = str(answer.get("evidence") or "")[:1000]
        decision.carrier = str(answer.get("carrier") or "")[:200]
        decision.reason = str(answer.get("reason") or "")[:1000]
        decision.error = ""
        with transaction.atomic():
            decision.save()
            _apply(decision)
    return results, completion


def _invalid(answer: dict) -> str:
    """Ответ проверяется сам: не все провайдеры гарантируют строгую JSON-схему."""
    if not isinstance(answer.get("present"), bool):
        return "chybí „present“"
    if answer.get("stance") not in Stance.values:
        return f"neznámý postoj {answer.get('stance')!r}"
    if not isinstance(answer.get("confidence"), int | float):
        return "chybí „confidence“"
    return ""


def _prototype_of(decision: LabelDecision) -> Prototype:
    narrative = decision.narrative
    return Prototype(
        narrative=narrative,
        version=decision.narrative_version,
        vector=np.zeros(1),
        patterns=[(cue, stem_pattern(fold(cue).strip())) for cue in narrative.cues if cue.strip()],
    )


def _apply(decision: LabelDecision) -> None:
    """Решение → метка модели (или её отсутствие) для версии нарратива."""
    labels = Label.objects.filter(
        document=decision.document,
        narrative_version=decision.narrative_version,
        origin=Label.Origin.MODEL,
    )
    if not decision.present or decision.confidence < float(get_setting("labeling-min-confidence")):
        labels.delete()
        return
    Label.objects.update_or_create(
        document=decision.document,
        narrative_version=decision.narrative_version,
        origin=Label.Origin.MODEL,
        defaults={
            "narrative": decision.narrative,
            "stance": decision.stance,
            "confidence": decision.confidence,
            "model_name": decision.model_name,
            "details": {
                "decision": decision.pk,
                "similarity": decision.similarity,
                "cues": decision.cues,
                "carrier": decision.carrier,
                "evidence": decision.evidence,
                "prompt": decision.prompt_version,
            },
        },
    )


# --- Очередь и бюджет -----------------------------------------------------------


def month_spend(today: datetime.date | None = None) -> float:
    today = today or timezone.localdate()
    start = today.replace(day=1)
    total = LabelDecision.objects.filter(decided_at__date__gte=start).aggregate(
        total=Sum("cost_usd")
    )["total"]
    return float(total or 0)


def pending() -> "LabelDecision.objects":
    return LabelDecision.objects.filter(present__isnull=True, error="").select_related(
        "document", "narrative", "narrative_version"
    )


@dataclass
class LabelStats:
    documents: int = 0
    decisions: int = 0
    labels: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    stopped: str = ""

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def label_pending(limit: int | None = None, time_budget: float | None = None) -> LabelStats:
    """Ожидающие решения → модель, по документам, в пределах месячного бюджета."""
    stats = LabelStats()
    if not llm.is_configured():
        stats.stopped = "chybí OPENROUTER_API_KEY"
        return stats
    model = str(get_setting("labeling-model"))
    budget = float(get_setting("labeling-monthly-budget-usd"))
    started = time.monotonic()
    while limit is None or stats.documents < limit:
        if month_spend() >= budget:
            stats.stopped = f"vyčerpán měsíční rozpočet {budget} USD"
            _notify_budget(budget)
            break
        first = pending().order_by("document_id").first()
        if first is None:
            break
        decisions = list(
            pending().filter(document_id=first.document_id).order_by("narrative__code")
        )
        try:
            decide_document(first.document, decisions, model)
            minimum = float(get_setting("labeling-min-confidence"))
            stats.labels += sum(1 for d in decisions if d.present and d.confidence >= minimum)
        except llm.LLMError as exc:
            LabelDecision.objects.filter(pk__in=[d.pk for d in decisions]).update(
                error=str(exc)[:1000]
            )
            stats.errors += len(decisions)
            logger.warning("Anotace dokumentu %s selhala: %s", first.document_id, exc)
        stats.documents += 1
        stats.decisions += len(decisions)
        if time_budget is not None and time.monotonic() - started > time_budget:
            break
    stats.cost_usd = round(month_spend(), 4)  # útrata za měsíc
    return stats


def _notify_budget(budget: float) -> None:
    from apps.panel.models import Notification

    title = f"Anotace narativů: vyčerpán měsíční rozpočet {budget} USD"
    month_start = timezone.localdate().replace(day=1)
    if not Notification.objects.filter(title=title, created_at__date__gte=month_start).exists():
        Notification.objects.create(
            notification_type=Notification.Type.JOB,
            level=Notification.Level.WARNING,
            title=title,
            text="Další kandidáti čekají na příští měsíc, nebo zvyšte nastavení "
            "„labeling-monthly-budget-usd“.",
        )


def retry_errors() -> int:
    """Решения с ошибкой (сеть, лимиты) снова в очередь."""
    return LabelDecision.objects.filter(present__isnull=True).exclude(error="").update(error="")


def refresh_relevance(center=None) -> int:
    """Перепроверка документов, отсеянных только порогом близости, по текущему кодбуку.

    Релевантность считалась по кодбуку на момент обработки: статьи о новом (особенно узком)
    нарративе могли тогда оказаться ниже порога. Эмбеддинги не пересчитываются. Документы,
    отсеянные фильтрами (пустой текст, качество 1, рубрика вне рамок), не трогаются.
    """
    from . import relevance

    center = get_center() if center is None else center
    if center is None:
        return 0
    codebook = relevance.load_codebook(model_name())
    revived = 0
    below = Document.objects.filter(
        is_relevant=False, embedding__isnull=False, relevance_details__reason="pod prahem"
    )
    for document in below.iterator(chunk_size=500):
        decision = relevance.decide(document, codebook, center)
        if decision["is_relevant"]:
            Document.objects.filter(pk=document.pk).update(
                is_relevant=True,
                relevance_score=decision["score"],
                relevance_details={**decision["details"], "revived_by_codebook": True},
            )
            revived += 1
    return revived


def queue_narrative(narrative: Narrative) -> int:
    """Ретроразметка: кандидаты новой версии нарратива по всему релевантному архиву."""
    prototypes = load_prototypes([narrative])
    center = get_center()
    refresh_relevance(center)
    created = 0
    documents = Document.objects.filter(is_relevant=True, embedding__isnull=False)
    for document in documents.iterator(chunk_size=500):
        created += queue_candidates(document, prototypes, center)
    return created
