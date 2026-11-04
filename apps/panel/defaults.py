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
        "Práh centrované kosinové podobnosti dokumentu s tématy a narativy (README §4, krok 3).",
    ),
    "reuse-verbatim-jaccard": (
        0.6,
        "Práh doslovného převzetí: Jaccard podle MinHash nebo podíl převzatých šindelů.",
    ),
    "reuse-paraphrase-cosine": (
        0.85,
        "Práh centrované kosinové podobnosti pro parafrázi (kNN). Kalibrace na médiích D: "
        "stejná událost psaná nezávisle 0,75–0,85, přepis jednoho textu 0,85–0,90.",
    ),
    "reuse-source-layers": (
        ["A"],
        "Vrstvy, ze kterých se hledá převzetí do ostatních vrstev (README §4, kroky 5–6).",
    ),
    "reuse-window-days": (30, "Převzetí: maximální odstup originálu a převzetí (dní)."),
    "minhash-shingle-size": (5, "Velikost šindele ve slovech (5–7)."),
    "narrative-rule-min-posts": (20, "Pravidlo přidání narativu: minimálně N příspěvků."),
    "narrative-rule-min-authors": (
        5,
        "Pravidlo přidání narativu: minimálně K nezávislých autorů.",
    ),
    "narrative-rule-window-days": (14, "Pravidlo přidání narativu: okno W (dní)."),
    "event-window-before-days": (
        14,
        "Event study (vrstva I, H4): okno před událostí (dní). Fixuje se předem (README §2.1).",
    ),
    "event-window-after-days": (14, "Event study (vrstva I, H4): okno po události (dní)."),
    "actor-min-activity": (4, "Minimum dokumentů za období pro zařazení aktéra."),
    "chart-click-max-lines": (10, "N: do kolika čar otevře kliknutí na bod grafu příspěvky."),
    "embedding-model": ("intfloat/multilingual-e5-base", "Model embeddingů (README §4, krok 4)."),
    "labeling-model": (
        "openai/gpt-6-luna",
        "Anotace narativů: model OpenRouter, který rozhoduje o kandidátech (README §4, krok 7). "
        "Vybrán srovnáním 4. 11. 2026 (manage.py label_narratives --compare).",
    ),
    "labeling-candidate-similarity": (
        0.25,
        "Anotace: kandidát, pokud je centrovaná podobnost s narativem aspoň tato.",
    ),
    "labeling-cue-similarity": (
        0.15,
        "Anotace: kandidát i při nižší podobnosti (aspoň tato), pokud text obsahuje "
        "2 a více signálních slov narativu.",
    ),
    "labeling-min-confidence": (
        0.5,
        "Anotace: štítek se vytvoří jen při jistotě modelu aspoň této; nižší zůstávají "
        "jako rozhodnutí k ruční kontrole.",
    ),
    "labeling-monthly-budget-usd": (
        5.0,
        "Anotace: měsíční strop útraty za model (USD). Po dosažení se další kandidáti nechají "
        "čekat a přijde upozornění.",
    ),
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
