"""Релевантность (README §4, шаг 3): дешёвые фильтры → ключевые слова → близость к кодбуку.

Нерелевантные документы не удаляются: они остаются знаменателем (README §4, шаг 3).
"""

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from apps.documents.models import Document, DocumentQuality
from apps.narratives.models import Narrative, NarrativeStatus, Topic
from apps.panel.defaults import get_setting

from .embeddings import centered, embed
from .text import fold

MIN_KEYWORDS = 2  # различных основ в тексте; в заголовке хватает одной
EMPTY_TEXT = 20

_prototype_cache: dict[str, np.ndarray] = {}


@dataclass
class Prototype:
    kind: str  # topic | narrative
    code: str
    topic: str
    text: str


@dataclass
class Codebook:
    """Снимок тем и активных нарративов на время пакета обработки."""

    topics: list[tuple[str, list[str]]] = field(default_factory=list)  # (код, основы)
    prototypes: list[Prototype] = field(default_factory=list)
    matrix: np.ndarray | None = None  # нецентрированные векторы прототипов

    @property
    def is_empty(self) -> bool:
        return not self.topics and not self.prototypes


def load_codebook(model: str) -> Codebook:
    codebook = Codebook()
    for topic in Topic.objects.all():
        stems = [fold(k).strip() for k in topic.keywords if str(k).strip()]
        if stems:
            codebook.topics.append((topic.code, stems))
        text = ". ".join(
            part for part in (topic.name, topic.description, ", ".join(topic.keywords)) if part
        )
        codebook.prototypes.append(Prototype("topic", topic.code, topic.code, text))
    for narrative in Narrative.objects.filter(status=NarrativeStatus.ACTIVE).select_related(
        "topic"
    ):
        examples = " ".join(str(e) for e in narrative.examples or [])
        text = f"{narrative.name}. {narrative.definition} {examples}".strip()
        codebook.prototypes.append(
            Prototype("narrative", narrative.code, narrative.topic.code, text)
        )
    if codebook.prototypes:
        codebook.matrix = _embed_cached([p.text for p in codebook.prototypes], model)
    return codebook


def _embed_cached(texts: list[str], model: str) -> np.ndarray:
    """Прототипы кодбука меняются редко — их векторы держатся в памяти воркера."""
    keys = [hashlib.sha256(f"{model}\n{t}".encode()).hexdigest() for t in texts]
    missing = [(k, t) for k, t in zip(keys, texts, strict=True) if k not in _prototype_cache]
    if missing:
        for (key, _), vector in zip(missing, embed([t for _, t in missing], model), strict=True):
            _prototype_cache[key] = vector
    return np.stack([_prototype_cache[k] for k in keys])


def gate(document: Document) -> str | None:
    """Дешёвые фильтры до эмбеддинга. Возвращает причину нерелевантности или None."""
    if len((document.text or "").strip()) < EMPTY_TEXT and len(document.title or "") < EMPTY_TEXT:
        return "prázdný text"
    score = (
        DocumentQuality.objects.filter(document=document, is_current=True)
        .values_list("score", flat=True)
        .first()
    )
    if score == 1:
        return "kvalita 1 (prázdná nebo blokovaná stránka)"
    if document.in_scope is False:  # рубрика вне исследования (apps.ingestion.scope)
        return f"rubrika mimo výzkum „{document.rubric}“"
    return None


@lru_cache(maxsize=1024)
def stem_pattern(stem: str) -> re.Pattern:
    """Основа — начало слова; «evropsk unie» найдёт и «evropské unie», и «Evropská unie».

    «=nato» — только целое слово (чешское «nato» = «затем» не должно давать «NATO» как основу).
    """
    exact = stem.startswith("=")
    body = r"\w*\s+".join(re.escape(part) for part in stem.lstrip("=").split())
    return re.compile(r"\b" + body + (r"\b" if exact else ""))


def keyword_hits(document: Document, codebook: Codebook) -> dict:
    """Найденные основы по темам: {код темы: {"text": [...], "title": [...]}}."""
    title, text = fold(document.title or ""), fold(document.text or "")
    hits = {}
    for code, stems in codebook.topics:
        in_text = sorted({s for s in stems if stem_pattern(s).search(text)})
        in_title = sorted({s for s in stems if stem_pattern(s).search(title)})
        if in_text or in_title:
            hits[code] = {"text": in_text, "title": in_title}
    return hits


def decide(document: Document, codebook: Codebook, center: np.ndarray | None) -> dict:
    """Решение по документу с эмбеддингом: is_relevant, score и подробности для панели."""
    if codebook.is_empty:
        return {"is_relevant": None, "score": None, "details": {"reason": "chybí témata"}}
    threshold = float(get_setting("relevance-threshold"))
    hits = keyword_hits(document, codebook)
    keyword_topics = [
        code for code, hit in hits.items() if hit["title"] or len(set(hit["text"])) >= MIN_KEYWORDS
    ]
    details: dict = {"keywords": hits} if hits else {}
    score = None
    semantic = False
    if center is not None and codebook.matrix is not None and document.embedding is not None:
        sims = centered(codebook.matrix, center) @ centered(document.embedding, center)[0]
        best = int(np.argmax(sims))
        score = round(float(sims[best]), 4)
        prototype = codebook.prototypes[best]
        details["nearest"] = {"kind": prototype.kind, "code": prototype.code, "similarity": score}
        details["topic"] = prototype.topic
        semantic = score >= threshold
        narratives = [
            (round(float(s), 4), p.code)
            for s, p in zip(sims, codebook.prototypes, strict=True)
            if p.kind == "narrative" and s >= threshold
        ]
        if narratives:
            details["narratives"] = [code for _, code in sorted(narratives, reverse=True)[:5]]
    else:
        details["semantic"] = "nedostupné (chybí střed prostoru embeddingů)"
    if keyword_topics and not semantic:
        details["topic"] = keyword_topics[0]
    reasons = []
    if keyword_topics:
        reasons.append("klíčová slova")
    if semantic:
        reasons.append("podobnost")
    details["reason"] = " + ".join(reasons) if reasons else "pod prahem"
    return {"is_relevant": bool(reasons), "score": score, "details": details}
