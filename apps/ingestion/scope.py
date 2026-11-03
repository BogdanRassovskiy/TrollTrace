"""Рубрики в рамках исследования (README §4, шаг 3; §5).

Общие ленты (Novinky, Seznam Zprávy, Blesk…) приносят и спорт, и знаменитостей. Сбор их
не отбрасывает (README §4.1: лучше лишнее, чем потерянное), но документ получает
Document.in_scope: в знаменатель долей и в воронку обработки идут только рубрики
из Source.collector_config["rubrics_in_scope"] (начала пути рубрики, без диакритики).
"rubrics_out_of_scope" — исключения внутри включённых (например, «Zprávy / Krimi»).
Без списка весь источник в рамках (ленты уже подключены по рубрикам).
"""

from apps.actors.models import Source
from apps.processing.text import fold


def _matches(rubric: str, prefixes: list[str]) -> bool:
    folded = " ".join(fold(rubric).split())
    return any(folded.startswith(" ".join(fold(p).split())) for p in prefixes if p.strip())


def rubric_in_scope(source: Source, rubric: str) -> bool | None:
    """True / False; None — рубрика не определена, а источник требует её проверки."""
    config = source.collector_config or {}
    include = config.get("rubrics_in_scope") or []
    exclude = config.get("rubrics_out_of_scope") or []
    if not include and not exclude:
        return True
    if not (rubric or "").strip():
        return None
    if _matches(rubric, exclude):
        return False
    return _matches(rubric, include) if include else True
