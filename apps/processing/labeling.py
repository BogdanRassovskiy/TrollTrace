"""Разметка нарративов (README §3; §4, шаг 7): кандидаты → решение LLM → метки.

1. Кандидаты. Для релевантного документа считается центрированная близость к каждому
   активному нарративу (название, определение, примеры) и ищутся сигнальные слова.
   Кандидат — близость ≥ `labeling-candidate-similarity` или ≥ `labeling-cue-similarity`
   при двух и более сигнальных словах. Пороги мягкие: пропуск дороже лишнего вызова.
   У нарративов, найденных автоматически (узкие формулировки), порог близости выше —
   `labeling-discovered-similarity`: иначе они цепляются за тему и почти всегда пусты.
   Вторая дорога — маркеры нарратива (apps/processing/markers.py): найденный в тексте
   маркер делает документ кандидатом при любой близости.
2. Решение. Один вызов LLM (OpenRouter, настройка `labeling-model`) на документ по всем его
   кандидатам: есть ли нарратив, позиция, уверенность, цитата-доказательство, кто нарратив
   несёт. Решения хранятся все, в том числе отрицательные (LabelDecision).
3. Метки. Положительное решение с уверенностью ≥ `labeling-min-confidence` → Label
   (origin = model) на версию нарратива. Ручные метки не трогаются.

4. Новые утверждения. Каждый новый релевантный документ проверяется моделью (Screening) в
   том же вызове: какие конкретные утверждения в нём есть, которых нет среди известных
   нарративов. Они складываются в кандидаты (apps/processing/novelty.py).

Близость эмбеддингов ловит тему, а не позицию, а локальная NLI-модель не понимает чешский
подтекст (проверено на примерах) — поэтому решение за LLM, а эмбеддинги только сужают поток.
Архив автоматически не переразмечается: нарратив отслеживается с момента появления, его
история — документы, из которых он вырос. Ретроразметка — только вручную (queue_narrative).
"""

import datetime
import logging
import time
from dataclasses import dataclass, field

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
    Screening,
    Stance,
)
from apps.panel.defaults import get_setting

from . import llm, markers
from .embeddings import centered, get_center, model_name
from .relevance import _embed_cached, stem_pattern
from .text import fold

logger = logging.getLogger(__name__)

PROMPT_VERSION = "p5"  # p2: přísná přítomnost; p3: nová tvrzení; p4: užší rámec; p5: markery
MIN_CUES = 2
HEAD_CHARS = 6000  # начало текста целиком
CUE_WINDOW = 400  # и окна вокруг сигнальных слов дальше по тексту
MAX_EXCERPT_CHARS = 9000
MAX_CLAIMS = 3
# Потолок ответа модели: без него OpenRouter резервирует под ответ максимум модели (65 536
# токенов) и при малом остатке кредитов отказывает с 402, хотя ответ — сотни токенов.
# Рассуждающие модели (deepseek) тратят часть потолка на скрытое рассуждение — до ~3 500
# токенов (7.11.2026); при 1 500–4 000 ответ обрывался посреди JSON.
ANSWER_TOKENS_BASE = 8000  # поиск новых утверждений, рассуждение и запас
ANSWER_TOKENS_PER_DECISION = 400  # решение: цитата ≤ 300 знаков, обоснование, ярлык
ANSWER_TOKENS_MAX = 16000
KNOWN_IN_PROMPT = 25  # názvy nejbližších sledovaných narativů, aby je model nehlásil jako nové
TRACKED = (NarrativeStatus.ACTIVE, NarrativeStatus.CANDIDATE)


@dataclass
class Prototype:
    narrative: Narrative
    version: NarrativeVersion
    vector: np.ndarray  # нецентрированный
    patterns: list
    markers: list = field(default_factory=list)  # [markers.Matcher]


def load_prototypes(narratives=None, marker_ids=None) -> list[Prototype]:
    """Прототипы отслеживаемых нарративов с их маркерами; marker_ids — только эти маркеры."""
    narratives = narratives or list(
        Narrative.objects.filter(status__in=TRACKED).select_related("topic")
    )
    if not narratives:
        return []
    texts = [f"{n.name}. {n.definition} {' '.join(map(str, n.examples))}" for n in narratives]
    vectors = _embed_cached(texts, model_name())
    index = markers.load_index([n.pk for n in narratives])
    return [
        Prototype(
            narrative=n,
            version=n.latest_version,
            vector=vector,
            patterns=[(cue, stem_pattern(fold(cue).strip())) for cue in n.cues if cue.strip()],
            markers=[
                m
                for m in index.for_narrative(n.pk)
                if marker_ids is None or m.marker_id in marker_ids
            ],
        )
        for n, vector in zip(narratives, vectors, strict=True)
    ]


def _cues(prototype: Prototype, folded: str) -> list[str]:
    return [cue for cue, pattern in prototype.patterns if pattern.search(folded)]


def find_candidates(document: Document, prototypes: list[Prototype], center,
                    markers_only: bool = False) -> list:  # fmt: skip
    """[(prototype, similarity, cues, matchers)] для релевантного документа с эмбеддингом.

    markers_only — только дорога маркеров (прогон архива по новым маркерам)."""
    if not document.is_relevant or document.embedding is None or center is None:
        return []
    high = float(get_setting("labeling-candidate-similarity"))
    high_discovered = float(get_setting("labeling-discovered-similarity"))
    low = float(get_setting("labeling-cue-similarity"))
    vector = centered(document.embedding, center)[0]
    folded = fold(f"{document.title}\n{document.text}")
    found = []
    for prototype in prototypes:
        similarity = round(float(centered(prototype.vector, center)[0] @ vector), 4)
        cues = _cues(prototype, folded)
        hits = markers.find(prototype.markers, folded)
        threshold = high_discovered if prototype.narrative.discovered else high
        by_meaning = similarity >= threshold or (similarity >= low and len(cues) >= MIN_CUES)
        if hits or (by_meaning and not markers_only):
            found.append((prototype, similarity, cues, hits))
    return found


def queue_candidates(document: Document, prototypes=None, center=None,
                     markers_only: bool = False) -> int:  # fmt: skip
    """Кандидаты документа → ожидающие решения. Повтор безопасен; уже решённые пары
    (тот же текст и та же версия нарратива) повторно модели не отправляются."""
    prototypes = load_prototypes() if prototypes is None else prototypes
    center = get_center() if center is None else center
    created = 0
    found = find_candidates(document, prototypes, center, markers_only)
    for prototype, similarity, cues, hits in found:
        stale = LabelDecision.objects.filter(
            document=document, narrative_version=prototype.version
        ).exclude(text_hash=document.text_hash)
        if stale.exists():  # текст изменился: прежние решения — история, метка пересчитается
            stale.filter(present__isnull=True).delete()
            Label.objects.filter(
                document=document, narrative_version=prototype.version, origin=Label.Origin.MODEL
            ).delete()
        marker_ids = [m.marker_id for m in hits]
        decision, new = LabelDecision.objects.get_or_create(
            document=document,
            narrative_version=prototype.version,
            text_hash=document.text_hash,
            defaults={
                "narrative": prototype.narrative,
                "similarity": similarity,
                "cues": cues,
                "markers": marker_ids,
            },
        )
        missing = [pk for pk in marker_ids if pk not in decision.markers]
        if not new and missing and decision.present is None:  # ещё ждёт модель
            decision.markers = [*decision.markers, *missing]
            decision.save(update_fields=["markers"])
        if new or missing:
            for matcher in hits:
                if matcher.remaining is not None:
                    matcher.remaining -= 1
        created += new
    return created


def drop_stale_labels(document: Document) -> int:
    """Текст документа изменился: метки модели, вынесенные по старому тексту, снимаются, даже
    если документ теперь нерелевантен и к модели больше не попадёт (иначе метка осталась бы
    навсегда). Если по текущему тексту уже есть решение, метка выставляется по нему.
    Решения остаются историей; ожидающие решения по старому тексту удаляются."""
    stale = LabelDecision.objects.filter(document=document).exclude(text_hash=document.text_hash)
    stale_ids = set(stale.values_list("pk", flat=True))
    if not stale_ids:
        return 0
    stale.filter(present__isnull=True).delete()
    dropped = 0
    for label in Label.objects.filter(document=document, origin=Label.Origin.MODEL):
        if (label.details or {}).get("decision") not in stale_ids:
            continue
        label.delete()
        dropped += 1
        current = (
            LabelDecision.objects.filter(
                document=document,
                text_hash=document.text_hash,
                narrative_version=label.narrative_version,
                present__isnull=False,
            )
            .order_by("-pk")
            .first()
        )
        if current is not None:
            _apply(current)
    return dropped


def is_new_text(document: Document) -> bool:
    """Текущий текст документа к модели ещё не попадал: нет ни проверки, ни решений по нему.

    Повторный проход воронки с тем же текстом очередь разметки не пополняет (README §3,
    «архив автоматически не переразмечается»): иначе каждый пересчёт ставил бы весь архив
    в очередь по нарративам, появившимся позже документа, и по новым версиям старых.
    """
    same = {"document": document, "text_hash": document.text_hash}
    return not (
        Screening.objects.filter(**same).exists() or LabelDecision.objects.filter(**same).exists()
    )


def queue_screening(document: Document) -> bool:
    """Новый релевантный документ → проверка моделью на новые утверждения."""
    _, created = Screening.objects.get_or_create(document=document, text_hash=document.text_hash)
    return created


# --- Решение LLM ---------------------------------------------------------------

SYSTEM = """You are a careful annotator in an academic research project (Jan Evangelista \
Purkyně University) that measures how narratives attributed to Russian influence operations \
spread in Czech media, Telegram channels and parliament, and in Russian state media \
(RT, RIA Novosti), whose texts are in Russian. You annotate; you do not fact-check.

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
- evidence: a verbatim quote (max 300 characters) from the document supporting the decision, \
in the document's own language (do not translate); empty string if not present.
- confidence: 0–1, how sure you are about present AND stance together.
- reason: one short sentence IN CZECH explaining the decision.
- marker: only when stance is "pro": the shortest verbatim expression (1–4 words) from the \
document that by itself carries this claim — typically a loaded label for a government, \
army, country or group (a name that presents a government as illegitimate, its soldiers as \
criminals, a people as nazis, a country as a puppet). Copy it exactly as written in the \
document. Empty string if the claim is argued only in full sentences, the expression is a \
neutral term, or stance is not "pro".

A loaded label used by the author in their own voice asserts the claim it encodes (e.g. \
calling the Ukrainian government a "junta" asserts that it is illegitimate): present = true, \
stance "pro", even without an explicit thesis. The same label only inside a quotation that \
the author does not endorse is "neutral".

Follow the stance guide of each narrative. Judge only the text given, not the source.
Answer with a JSON object that follows the given schema."""

DISCOVERY = """

Additionally list NEW CLAIMS ("claims"): specific claims (not topics) that the document \
makes or reports and that are NOT covered by any narrative listed in the codebook or under \
"Other tracked narratives". Only claims typical of influence narratives: claims that \
delegitimise or vilify Ukraine, the EU, NATO, the USA or the West, Czech democratic \
institutions, elections, mainstream media or support for Ukraine; that present Russia or the \
USSR favourably or justify their actions; or that spread fear (war, energy, migration, \
economic collapse) to undermine trust in the state. Do NOT list:
  - factual reporting of events (attacks, battles, votes, statements) without an \
interpretive claim of that kind;
  - ordinary criticism of a government, party or politician's policy (e.g. "the government \
spends too little on defence"), which is normal political debate;
  - claims about other countries' domestic affairs that do not concern Czechia, the EU, \
NATO, Ukraine or Russia;
  - claims mentioned only in passing.
For each claim:
- claim: one declarative sentence IN CZECH stating the claim in a general form, so that other \
texts repeating the same claim would match (no outlet names, no quotation marks). Keep \
incident-specific details only when the claim is about one incident.
- stance: "pro" if the document pushes the claim, "neutral" if it reports someone making it, \
"contra" if it explicitly refutes it.
- carrier, evidence, confidence as above.
At most 3 claims; an empty list if there are none."""


def _excerpt(document: Document, prototypes: list[Prototype]) -> str:
    text = document.text or ""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    parts, used = [text[:HEAD_CHARS]], HEAD_CHARS
    folded = fold(text)  # fold zachovává délku pro češtinu (diakritika se jen odstraní)
    for prototype in prototypes:
        patterns = [p for _, p in prototype.patterns] + [m.pattern for m in prototype.markers]
        for pattern in patterns:
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


def build_prompt(document: Document, prototypes: list[Prototype], known=()) -> str:
    published = document.published_at.date().isoformat() if document.published_at else "?"
    codebook = "\n\n".join(_narrative_block(p.narrative) for p in prototypes) or "(žádný)"
    other = ""
    if known:
        other = "## Other tracked narratives\n\n" + "\n".join(f"- {n}" for n in known) + "\n\n"
    return (
        f"## Codebook\n\n{codebook}\n\n{other}## Document\n\nDatum: {published}\n"
        f"Titulek: {document.title}\n\n{_excerpt(document, prototypes)}"
    )


def known_names(document: Document, exclude: set[str], prototypes=None, center=None) -> list:
    """Названия ближайших отслеживаемых нарративов вне кандидатов документа (для поиска новых)."""
    prototypes = load_prototypes() if prototypes is None else prototypes
    others = [p for p in prototypes if p.narrative.code not in exclude]
    if center is not None and document.embedding is not None and others:
        vector = centered(document.embedding, center)[0]
        others.sort(key=lambda p: -float(centered(p.vector, center)[0] @ vector))
    return [p.narrative.name for p in others[:KNOWN_IN_PROMPT]]


def schema(codes: list[str], discovery: bool = False) -> dict:
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
            "marker": {"type": "string"},
        },
        "required": ["narrative", "present", "stance", "confidence", "carrier", "evidence",
                     "reason", "marker"],
        "additionalProperties": False,
    }  # fmt: skip
    if not codes:
        item["properties"]["narrative"] = {"type": "string"}
    properties = {"decisions": {"type": "array", "items": item}}
    if discovery:
        claim = {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "stance": {"type": "string", "enum": list(Stance.values)},
                "confidence": {"type": "number"},
                "carrier": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["claim", "stance", "confidence", "carrier", "evidence"],
            "additionalProperties": False,
        }
        properties["claims"] = {"type": "array", "items": claim}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def decide_document(document: Document, decisions: list[LabelDecision], model: str,
                    client=None, *, screening: Screening | None = None, known=(),
                    save: bool = True):  # fmt: skip
    """Один вызов модели по всем ожидающим решениям документа (и поиск новых утверждений,
    если документ ждёт проверки `screening`).

    Возвращает (ответы модели по решениям, Completion). save=False — только сравнение моделей.
    """
    prototypes = [_prototype_of(d) for d in decisions]
    codes = [p.narrative.code for p in prototypes]
    discovery = screening is not None
    completion = llm.complete_json(
        model,
        SYSTEM + (DISCOVERY if discovery else ""),
        build_prompt(document, prototypes, known if discovery else ()),
        schema(codes, discovery),
        client=client,
        max_tokens=answer_tokens(len(decisions)),
        reasoning_effort=str(get_setting("labeling-reasoning-effort") or ""),
    )
    data = completion.data if isinstance(completion.data, dict) else {}
    items = data.get("decisions")
    answers = {a.get("narrative"): a for a in items or [] if isinstance(a, dict)}
    parts = len(decisions) + discovery
    cost = completion.cost_usd / parts if completion.cost_usd is not None else None
    if discovery and save:
        from . import novelty

        screening.model_name, screening.prompt_version = model, PROMPT_VERSION
        screening.cost_usd, screening.checked_at, screening.error = cost, timezone.now(), ""
        with transaction.atomic():
            screening.n_claims = novelty.record_claims(screening, data.get("claims"))
            screening.save()
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
    if save:
        markers.after_decisions(decisions, results)
    return results, completion


def answer_tokens(n_decisions: int) -> int:
    return min(ANSWER_TOKENS_MAX, ANSWER_TOKENS_BASE + ANSWER_TOKENS_PER_DECISION * n_decisions)


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
    found = markers.NarrativeMarker.objects.filter(pk__in=decision.markers or [])
    return Prototype(
        narrative=narrative,
        version=decision.narrative_version,
        vector=np.zeros(1),
        patterns=[(cue, stem_pattern(fold(cue).strip())) for cue in narrative.cues if cue.strip()],
        markers=[markers.Matcher(m.pk, m.key, stem_pattern(m.key)) for m in found],
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
                "markers": decision.markers,
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
    decisions = LabelDecision.objects.filter(decided_at__date__gte=start)
    screenings = Screening.objects.filter(checked_at__date__gte=start)
    return sum(
        float(qs.aggregate(total=Sum("cost_usd"))["total"] or 0) for qs in (decisions, screenings)
    )


def pending() -> "LabelDecision.objects":
    return LabelDecision.objects.filter(present__isnull=True, error="").select_related(
        "document", "narrative", "narrative_version"
    )


def pending_screenings() -> "Screening.objects":
    return Screening.objects.filter(checked_at__isnull=True, error="")


def next_document_id() -> int | None:
    """Следующий документ очереди: ждёт решения по кандидатам или проверки."""
    ids = [
        qs.order_by("document_id").values_list("document_id", flat=True).first()
        for qs in (pending(), pending_screenings())
    ]
    ids = [i for i in ids if i is not None]
    return min(ids) if ids else None


@dataclass
class LabelStats:
    documents: int = 0
    decisions: int = 0
    labels: int = 0
    claims: int = 0
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
    if get_setting("labeling-paused"):
        stats.stopped = "anotace pozastavena (nastavení labeling-paused)"
        return stats
    model = str(get_setting("labeling-model"))
    budget = float(get_setting("labeling-monthly-budget-usd"))
    started = time.monotonic()
    prototypes, center = load_prototypes(), get_center()
    while limit is None or stats.documents < limit:
        if month_spend() >= budget:
            stats.stopped = f"vyčerpán měsíční rozpočet {budget} USD"
            _notify_budget(budget)
            break
        document_id = next_document_id()
        if document_id is None:
            break
        document = Document.objects.get(pk=document_id)
        decisions = list(pending().filter(document_id=document_id).order_by("narrative__code"))
        screening = pending_screenings().filter(document_id=document_id).first()
        if screening is not None and screening.text_hash != document.text_hash:
            screening.delete()  # text se změnil; novou kontrolu založí zpracování dokumentu
            screening = None
            if not decisions:
                continue
        known = []
        if screening is not None:
            exclude = {d.narrative.code for d in decisions}
            known = known_names(document, exclude, prototypes, center)
        try:
            decide_document(document, decisions, model, screening=screening, known=known)
            minimum = float(get_setting("labeling-min-confidence"))
            stats.labels += sum(1 for d in decisions if d.present and d.confidence >= minimum)
            stats.claims += screening.n_claims if screening is not None else 0
        except llm.LLMError as exc:
            LabelDecision.objects.filter(pk__in=[d.pk for d in decisions]).update(
                error=str(exc)[:1000]
            )
            if screening is not None:
                Screening.objects.filter(pk=screening.pk).update(error=str(exc)[:1000])
            stats.errors += len(decisions) + (screening is not None)
            logger.warning("Anotace dokumentu %s selhala: %s", document_id, exc)
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
    """Решения и проверки с ошибкой (сеть, лимиты) снова в очередь."""
    decisions = (
        LabelDecision.objects.filter(present__isnull=True).exclude(error="").update(error="")
    )
    return decisions + Screening.objects.filter(checked_at__isnull=True).exclude(error="").update(
        error=""
    )


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
    """Ручная ретроразметка: кандидаты текущей версии нарратива по всему релевантному архиву.

    Автоматически не запускается (новые нарративы отслеживаются с момента появления); нужна,
    например, для нарратива из литературы, добавленного задним числом.
    """
    prototypes = load_prototypes([narrative])
    center = get_center()
    refresh_relevance(center)
    created = 0
    documents = Document.objects.filter(is_relevant=True, embedding__isnull=False)
    for document in documents.iterator(chunk_size=500):
        created += queue_candidates(document, prototypes, center)
    return created
