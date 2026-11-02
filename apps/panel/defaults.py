"""Стартовые значения настроек исследования. Меняются в панели, каждое изменение версионируется."""

from .models import Setting

DEFAULT_SETTINGS: dict[str, tuple[object, str]] = {
    "aggregation-window": ("week", "Окно агрегации (README §5)."),
    "analysis-min-quality": (
        3,
        "Минимальная оценка качества документа для агрегатов (README §4.3).",
    ),
    "relevance-threshold": (
        0.35,
        "Порог сходства эмбеддинга с описаниями нарративов (README §4, шаг 3).",
    ),
    "reuse-verbatim-jaccard": (0.6, "Порог Jaccard по MinHash для дословного заимствования."),
    "reuse-paraphrase-cosine": (0.85, "Порог косинусного сходства для пересказа (kNN)."),
    "minhash-shingle-size": (5, "Размер шингла в словах (5–7)."),
    "narrative-rule-min-posts": (20, "Правило добавления нарратива: минимум N постов."),
    "narrative-rule-min-authors": (
        5,
        "Правило добавления нарратива: минимум K независимых авторов.",
    ),
    "narrative-rule-window-days": (14, "Правило добавления нарратива: окно W (дней)."),
    "actor-min-activity": (4, "Минимум документов за период для включения актора."),
    "chart-click-max-lines": (10, "N: до скольких линий клик по точке графика открывает посты."),
    "embedding-model": ("intfloat/multilingual-e5-base", "Модель эмбеддингов (README §4, шаг 4)."),
}


def ensure_default_settings() -> int:
    """Создаёт отсутствующие настройки, существующие не трогает. Возвращает число созданных."""
    created = 0
    for key, (value, description) in DEFAULT_SETTINGS.items():
        if not Setting.objects.filter(key=key).exists():
            Setting(key=key, value=value, description=description).save(
                change_note="Стартовое значение"
            )
            created += 1
    return created
