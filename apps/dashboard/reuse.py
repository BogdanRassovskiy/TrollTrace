"""Подсветка совпадающих фрагментов в паре текстов (просмотр заимствований, README §7.1).

Совпадения — общие последовательности не короче `min_words` слов (difflib по словам после
нормализации: регистр и диакритика не мешают совпадению). Возвращаются части текста для
шаблона: [(фрагмент, совпадает ли)].
"""

import re
from difflib import SequenceMatcher

from apps.processing.text import fold

TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_WORDS = 20000  # velmi dlouhé texty: porovnává se začátek


def _tokens(text: str) -> list[re.Match]:
    return list(TOKEN.finditer(text))[:MAX_WORDS]


def _parts(text: str, tokens: list[re.Match], spans: list[tuple[int, int]]) -> list:
    """spans — интервалы индексов слов → части текста с флагом совпадения."""
    parts, cursor = [], 0
    for start, end in sorted(spans):
        a, b = tokens[start].start(), tokens[end - 1].end()
        if a > cursor:
            parts.append((text[cursor:a], False))
        parts.append((text[a:b], True))
        cursor = b
    if cursor < len(text):
        parts.append((text[cursor:], False))
    return parts


def highlight(source: str, target: str, min_words: int = 5):
    """→ (части оригинала, части převzetí, доля слов převzetí, совпадающих с оригиналом)."""
    a_tokens, b_tokens = _tokens(source), _tokens(target)
    a_words = [fold(t.group()).lower() for t in a_tokens]
    b_words = [fold(t.group()).lower() for t in b_tokens]
    matcher = SequenceMatcher(None, a_words, b_words, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= min_words]
    a_spans = [(b.a, b.a + b.size) for b in blocks]
    b_spans = [(b.b, b.b + b.size) for b in blocks]
    matched = sum(b.size for b in blocks)
    share = matched / len(b_words) if b_words else 0.0
    return _parts(source, a_tokens, a_spans), _parts(target, b_tokens, b_spans), share
