"""Нормализация текста для ключевых слов и шинглов: нижний регистр, без диакритики."""

import re
import unicodedata

_WORD = re.compile(r"\w+", re.UNICODE)


def fold(text: str) -> str:
    """«Uprchlíků» → «uprchliku». Ключевые слова кодбука пишутся без диакритики."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))
