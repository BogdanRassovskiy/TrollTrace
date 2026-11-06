"""Кандидаты в нарративы: фрагменты текста для решения исследователя.

Чтобы принять или отклонить кластер, мало формулировки модели: нужно видеть, как утверждение
стоит в самих документах — не сарказм ли это, не ирония ли, не чужая ли цитата (которую автор
пересказывает или опровергает). Поэтому к каждому кандидату подбирается несколько фрагментов:
цитата-доклад модели, найденная в тексте документа и показанная с соседними предложениями.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from django.db.models import F, Q

from apps.narratives.models import NarrativeCandidate, NovelClaim

FRAGMENTS = 3
CONTEXT_CHARS = 280  # сколько текста показывать до и после найденного места
# конец предложения: знак препинания (возможно, с закрывающей кавычкой) и пробел, или абзац
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][“”\"»])\s+|\n+")
AUTHOR_WORDS = {"", "autor", "autor textu", "autor článku", "redakce", "text", "dokument"}


@dataclass
class Fragment:
    claim: NovelClaim
    before: str
    match: str
    after: str
    found: bool  # доклад найден в тексте (иначе показан только доклад модели)
    in_quotes: bool  # найденное место стоит в кавычках
    by_author: bool  # по ответу модели утверждение несёт сам автор, а не цитируемое лицо

    @property
    def is_quote(self) -> bool:
        return self.in_quotes or not self.by_author


def _squash(text: str) -> str:
    """Пробелы схлопываются, абзацы остаются (кавычки ищутся в пределах абзаца)."""
    text = re.sub(r"[^\S\n]+", " ", text or "")
    return re.sub(r" ?\n[\s]*", "\n", text).strip()


def _locate(text: str, evidence: str) -> tuple[int, int] | None:
    """Положение доклада в тексте: точное совпадение без учёта регистра, иначе самое похожее
    предложение (модель цитирует не всегда дословно)."""
    if not evidence:
        return None
    start = text.casefold().find(evidence.casefold())
    if start >= 0:
        return start, start + len(evidence)
    best, best_ratio, position = None, 0.0, 0
    for sentence in SENTENCE_END.split(text):
        start = text.find(sentence, position)
        position = max(position, start)
        if len(sentence) < 15:
            continue
        ratio = SequenceMatcher(None, sentence.casefold(), evidence.casefold()).ratio()
        if ratio > best_ratio:
            best, best_ratio = (start, start + len(sentence)), ratio
    return best if best_ratio >= 0.6 else None


def _in_quotes(text: str, start: int) -> bool:
    """Открыта ли кавычка перед местом (в пределах абзаца)."""
    depth, straight = 0, False
    for char in text[text.rfind("\n", 0, start) + 1 : start]:
        if char in "„«":
            depth += 1
        elif char in "”»":
            depth = max(0, depth - 1)
        elif char == "“":  # в чешском закрывает „, в английском открывает
            depth = depth - 1 if depth else 1
        elif char == '"':
            straight = not straight
    return depth > 0 or straight


def _by_author(claim: NovelClaim) -> bool:
    carrier = claim.carrier.strip().casefold()
    document = claim.document
    own = {document.actor.name.casefold(), document.source.name.casefold()}
    return carrier in AUTHOR_WORDS or carrier in own or carrier.startswith("autor")


def fragment(claim: NovelClaim) -> Fragment:
    text = _squash(claim.document.text)
    evidence = _squash(claim.evidence)
    span = _locate(text, evidence)
    if span is None:
        return Fragment(claim, "", evidence or claim.claim, "", False, False, _by_author(claim))
    start, end = span
    before, after = text[max(0, start - CONTEXT_CHARS) : start], text[end : end + CONTEXT_CHARS]
    if start > CONTEXT_CHARS:
        before = "…" + before.split(" ", 1)[-1]
    if end + CONTEXT_CHARS < len(text):
        after = after.rsplit(" ", 1)[0] + "…"
    return Fragment(
        claim, before, text[start:end], after, True, _in_quotes(text, start), _by_author(claim)
    )


def fragments(candidate: NarrativeCandidate, limit: int = FRAGMENTS) -> list[Fragment]:
    """Несколько фрагментов из разных источников: сначала по одному на источник (с докладом,
    самые уверенные), затем остальные документы."""
    claims = list(
        candidate.claims.select_related("document__source", "document__actor").order_by(
            "-confidence", "pk"
        )
    )
    claims.sort(key=lambda c: not c.evidence)  # с докладом — вперёд (сортировка устойчива)
    chosen, sources, documents = [], set(), set()
    for distinct_source in (True, False):
        for claim in claims:
            if len(chosen) >= limit:
                break
            if claim.document_id in documents:
                continue
            if distinct_source and claim.document.source_id in sources:
                continue
            chosen.append(claim)
            sources.add(claim.document.source_id)
            documents.add(claim.document_id)
    return [fragment(claim) for claim in chosen]


def visible_q() -> Q:
    """Видимый кандидат: уже отслеживается или набрал порог видимости (README «Дозревание»)."""
    from apps.panel.defaults import get_setting

    return Q(tracked_since__isnull=False) | Q(
        n_documents__gte=int(get_setting("discovery-visible-min-posts")),
        n_authors__gte=int(get_setting("discovery-visible-min-authors")),
    )


def open_candidates(incubator: bool = False):
    """Ждут решения: сначала уже отслеживаемые (выполнили правило), затем по числу авторов.
    incubator=True — малые кластеры ниже порога видимости (копятся молча, дозревают сами)."""
    Status = NarrativeCandidate.Status
    candidates = NarrativeCandidate.objects.filter(status__in=[Status.NEW, Status.DEFERRED])
    candidates = candidates.exclude(visible_q()) if incubator else candidates.filter(visible_q())
    return candidates.select_related("suggested_narrative").order_by(
        F("tracked_since").asc(nulls_last=True), "-n_authors", "-n_documents", "-pk"
    )
