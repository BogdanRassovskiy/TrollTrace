"""Стартовые значения настроек исследования. Меняются в панели, каждое изменение версионируется."""

from .models import Setting

DEFAULT_SETTINGS: dict[str, tuple[object, str]] = {
    "aggregation-window": ("week", "Okno agregace (README §5)."),
    "aggregation-z-window": (
        12,
        "Agregace: z-skóre vůči posledním N týdnům vlastní historie řady (výkyvy u aktéra).",
    ),
    "aggregation-z-min-weeks": (
        4,
        "Agregace: z-skóre se počítá, až má řada aspoň tolik předchozích týdnů s dokumenty.",
    ),
    "aggregation-prior-strength": (
        5,
        "Agregace: síla bayesovského vyhlazení podílu k podílu vrstvy (v počtu dokumentů); "
        "u málo píšících aktérů tlumí výkyvy z jednoho příspěvku.",
    ),
    "analysis-min-quality": (
        3,
        "Minimální hodnocení kvality dokumentu pro agregáty (README §4.3).",
    ),
    "relevance-threshold": (
        0.35,
        "Práh centrované kosinové podobnosti dokumentu s tématy a narativy (README §4, krok 3).",
    ),
    "relevance-all-layers": (
        ["Z"],
        "Vrstvy, jejichž dokumenty jsou po levných filtrech celé relevantní. Vrstva Z: sbírají se "
        "jen rubriky o politice a světě a česká klíčová slova kódové knihy na ruské texty "
        "nezabírají — o narativech rozhoduje až model (rozhodnutí výzkumníka 6. 11. 2026).",
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
    "narrative-rule-min-posts": (
        20,
        "Pravidlo přidání narativu: minimálně N dokumentů s novým tvrzením. Splněný shluk se "
        "začne sledovat jako narativ ve stavu kandidát.",
    ),
    "narrative-rule-min-authors": (
        5,
        "Pravidlo přidání narativu: minimálně K nezávislých autorů (aktér, jinak zdroj).",
    ),
    "narrative-rule-window-days": (14, "Pravidlo přidání narativu: okno W (dní)."),
    "discovery-known-similarity": (
        0.62,
        "Nová tvrzení: tvrzení aspoň takto podobné názvu nebo příkladu sledovaného narativu "
        "není nové, dokument se posoudí podle tohoto narativu (centrovaná kosinová podobnost; "
        "kalibrace: shody 0,65–0,72, ostatní ≤ 0,57).",
    ),
    "discovery-cluster-similarity": (
        0.65,
        "Nová tvrzení: aspoň takto podobné tvrzení se přidá do existujícího shluku, jinak "
        "založí nový. Kalibrace 6.11.2026 na 172 shlucích: páry 0,65–0,75 jsou většinou totéž "
        "tvrzení jinými slovy (DSA, Agrofert, věznice Azov), páry 0,60–0,65 jen totéž téma; "
        "dříve 0,75 — totéž tvrzení se drobilo do mnoha jednodokumentových shluků.",
    ),
    "discovery-maturation-days": (
        14,
        "Dozrávání kandidátů: shluk bez nového dokumentu tolik dní se posoudí. Malý shluk model "
        "přiřadí k existujícímu narativu (dokumenty se posoudí podle něj), jinak vyprší; "
        "viditelný shluk dostane jen návrh, rozhoduje výzkumník.",
    ),
    "discovery-visible-min-posts": (
        3,
        "Fronta kandidátů: výzkumníkovi se ukazují shluky s aspoň N dokumenty (menší se tiše "
        "sbírají v inkubátoru a po dozrání se přiřadí nebo vyprší).",
    ),
    "discovery-visible-min-authors": (
        2,
        "Fronta kandidátů: … a od aspoň K nezávislých autorů.",
    ),
    "discovery-maturation-batch": (
        300,
        "Dozrávání: nejvýše tolik shluků za jeden běh (jeden levný dotaz na model za shluk).",
    ),
    "event-window-before-days": (
        14,
        "Event study (vrstva I, H4): okno před událostí (dní). Fixuje se předem (README §2.1).",
    ),
    "event-window-after-days": (14, "Event study (vrstva I, H4): okno po události (dní)."),
    "actor-min-activity": (4, "Minimum dokumentů za období pro zařazení aktéra."),
    "chart-click-max-lines": (10, "N: do kolika čar otevře kliknutí na bod grafu příspěvky."),
    "spike-z-threshold": (
        2.0,
        "Výkyv u aktéra: týden se z-skóre aspoň tímto se v grafech a seznamu aktérů označí.",
    ),
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
