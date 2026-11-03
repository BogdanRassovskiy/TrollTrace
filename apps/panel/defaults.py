"""Стартовые значения настроек исследования. Меняются в панели, каждое изменение версионируется."""

from .models import Setting

DEFAULT_SETTINGS: dict[str, tuple[object, str]] = {
    "aggregation-window": ("week", "Okno agregace (README §5)."),
    "analysis-min-quality": (
        3,
        "Minimální hodnocení kvality dokumentu pro agregáty (README §4.3).",
    ),
    "relevance-threshold": (
        0.35,
        "Práh podobnosti embeddingu s popisy narativů (README §4, krok 3).",
    ),
    "reuse-verbatim-jaccard": (0.6, "Práh Jaccard podle MinHash pro doslovné převzetí."),
    "reuse-paraphrase-cosine": (0.85, "Práh kosinové podobnosti pro parafrázi (kNN)."),
    "minhash-shingle-size": (5, "Velikost šindele ve slovech (5–7)."),
    "narrative-rule-min-posts": (20, "Pravidlo přidání narativu: minimálně N příspěvků."),
    "narrative-rule-min-authors": (
        5,
        "Pravidlo přidání narativu: minimálně K nezávislých autorů.",
    ),
    "narrative-rule-window-days": (14, "Pravidlo přidání narativu: okno W (dní)."),
    "actor-min-activity": (4, "Minimum dokumentů za období pro zařazení aktéra."),
    "chart-click-max-lines": (10, "N: do kolika čar otevře kliknutí na bod grafu příspěvky."),
    "embedding-model": ("intfloat/multilingual-e5-base", "Model embeddingů (README §4, krok 4)."),
    "monitoring-volume-ratio": (
        0.33,
        "Monitoring: signál, pokud je dokumentů za den méně než tento podíl mediánu (README §4.2).",
    ),
    "monitoring-min-mean-quality": (
        3.5,
        "Monitoring: signál, pokud je průměrné hodnocení kvality za den nižší (README §4.3).",
    ),
}


def get_setting(key: str):
    """Текущее значение настройки; если её ещё нет в БД — стартовое."""
    value = Setting.objects.filter(key=key).values_list("value", flat=True).first()
    return DEFAULT_SETTINGS[key][0] if value is None else value


def ensure_default_settings() -> int:
    """Создаёт отсутствующие настройки, существующие не трогает. Возвращает число созданных."""
    created = 0
    for key, (value, description) in DEFAULT_SETTINGS.items():
        if not Setting.objects.filter(key=key).exists():
            Setting(key=key, value=value, description=description).save(
                change_note="Výchozí hodnota"
            )
            created += 1
    return created
