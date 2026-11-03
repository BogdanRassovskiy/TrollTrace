"""Оценка качества данных документа 1–5 (README §4.3): сначала флаги, потом оценка.

Оценка — о технической корректности сбора и извлечения, не о содержании. Считается при
сохранении документа и при каждой новой версии; прежние оценки остаются в истории.
При изменении правил меняется RULES_VERSION и оценки пересчитываются (assess_quality --all).
"""

import datetime
import re
from collections import Counter

from django.utils import timezone

from apps.actors.models import Platform
from apps.documents.models import Document, DocumentQuality

RULES_VERSION = "q1"

EMPTY_TEXT = 20  # символов: короче — текста нет (у статей)
EMPTY_TEXT_SHORT_FORM = 3  # реплики в стенограммах и посты Telegram бывают в одно слово
FUTURE_TOLERANCE = datetime.timedelta(hours=1)
DEFAULT_MAX_DATE_LAG_DAYS = 30  # дальше от collected_at — подозрительно (кроме ретро-сбора)
MIN_TEXT_TO_HTML = 0.001  # доля текста в HTML; меньше — вероятно, взят не тот блок
RATIO_SAFE_LENGTH = 1500  # длинный текст считаем извлечённым верно при любой доле

# Капча, антибот, paywall, согласие с cookies, страницы ошибок (cs / en / ru).
BLOCKED_PATTERNS = re.compile(
    r"captcha|cloudflare|just a moment|attention required|access denied|ověřte, že nejste robot|"
    r"nejste robot|enable javascript|povolte javascript|please turn javascript on|"
    r"souhlas s (?:použitím|zpracováním) cookies|přijmout vše|accept all cookies|"
    r"pro pokračování .{0,40}(?:předplat|přihlas)|obsah je dostupný pouze pro předplatitele|"
    r"stránka nebyla nalezena|page not found|404 not found|503 service|доступ запрещ",
    re.IGNORECASE,
)

# Частые служебные слова: грубое, но достаточное определение языка для проверки lang_ok.
STOPWORDS = {
    "cs": {"a", "je", "se", "na", "že", "to", "v", "ve", "jsou", "který", "také", "pro", "jako",
           "by", "byl", "ale", "jeho", "podle", "už", "když", "není", "bude", "aby"},
    "sk": {"a", "je", "sa", "na", "že", "to", "v", "vo", "sú", "ktorý", "tiež", "pre", "ako",
           "by", "bol", "ale", "jeho", "podľa", "už", "keď", "nie", "bude", "aby"},
    "ru": {"и", "в", "не", "на", "что", "с", "по", "это", "как", "из", "для", "он", "но",
           "а", "к", "о", "его", "так", "же", "от", "который", "был", "будет"},
    "uk": {"і", "в", "не", "на", "що", "з", "це", "як", "до", "для", "він", "але", "та",
           "а", "й", "у", "його", "так", "же", "від", "який", "був", "буде"},
    "en": {"the", "and", "of", "to", "in", "is", "that", "for", "it", "with", "as", "was",
           "on", "be", "by", "this", "are", "from", "at", "has", "have", "will", "not"},
}  # fmt: skip
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str) -> str:
    """Язык по служебным словам; «» — если текста мало или уверенности нет."""
    words = [w.lower() for w in WORD.findall(text[:5000])]
    if len(words) < 20:
        return ""
    counts = Counter(words)
    scores = {lang: sum(counts[w] for w in stop) for lang, stop in STOPWORDS.items()}
    lang, best = max(scores.items(), key=lambda kv: kv[1])
    if best < len(words) * 0.05:
        return ""
    # cs и sk близки: различаем по характерным словам.
    if lang in {"cs", "sk"}:
        sk_only = sum(counts[w] for w in ("sa", "sú", "ktorý", "podľa", "keď", "nie", "pre"))
        cs_only = sum(counts[w] for w in ("se", "jsou", "který", "podle", "když", "není", "pro"))
        lang = "sk" if sk_only > cs_only else "cs"
    return lang


def _max_date_lag(document: Document) -> datetime.timedelta | None:
    config = document.source.collector_config or {}
    if document.source.platform == Platform.PARLIAMENT or config.get("retro"):
        return None  # стенограммы и ретро-сбор: старые даты — норма
    return datetime.timedelta(days=int(config.get("max_date_lag_days", DEFAULT_MAX_DATE_LAG_DAYS)))


def compute_flags(document: Document, raw_html: str = "", incident_level: str = "") -> dict:
    source = document.source
    text, title = document.text.strip(), document.title.strip()
    min_length = source.min_text_length
    empty_below = EMPTY_TEXT if source.platform == Platform.WEB else EMPTY_TEXT_SHORT_FORM

    published, collected = document.published_at, document.collected_at or timezone.now()
    date_plausible = False
    if published is not None:
        lag = _max_date_lag(document)
        date_plausible = published <= collected + FUTURE_TOLERANCE and (
            lag is None or collected - published <= lag
        )

    expected = (source.expected_language or "").lower()
    language = (document.language or detect_language(text)).lower()
    lang_ok = not expected or not language or language == expected

    head = f"{title}\n{text[:1000]}"
    blocked = len(text) < max(min_length, 300) * 2 and bool(BLOCKED_PATTERNS.search(head))

    ratio_ok = (
        not raw_html
        or len(text) >= RATIO_SAFE_LENGTH
        or len(text) >= len(raw_html) * MIN_TEXT_TO_HTML
    )
    return {
        "has_title": bool(title),
        "has_published_at": published is not None,
        "date_plausible": date_plausible,
        "text_ok": len(text) >= min_length,
        "text_short": empty_below <= len(text) < min_length,
        "text_empty": len(text) < empty_below,
        "lang_ok": lang_ok,
        "boilerplate_dup": bool((document.meta or {}).get("duplicate_urls")),
        "blocked_page": blocked,
        "selectors_ok": None,  # собственных селекторов пока нет — только trafilatura
        "extraction_ratio_ok": ratio_ok,
        "incident_level": incident_level,
    }


def score(flags: dict, *, date_source: str, rubric_expected: bool, has_rubric: bool,
          text_length: int, min_length: int, short_form: bool = False) -> int:  # fmt: skip
    """Детерминированные правила шкалы README §4.3."""
    if flags["text_empty"] or flags["boilerplate_dup"] or flags["blocked_page"]:
        return 1

    severe = [
        not flags["lang_ok"],
        flags["selectors_ok"] is False,
        flags["incident_level"] == DocumentQuality.IncidentLevel.CRITICAL,
    ]
    # Реплики и посты бывают короткими по природе: для них длина — мелкий изъян, не проблема.
    much_shorter = flags["text_short"] and not short_form and text_length < min_length / 2
    major = [
        not flags["has_published_at"] or not flags["date_plausible"],
        much_shorter,
        not flags["extraction_ratio_ok"],
        flags["incident_level"] == DocumentQuality.IncidentLevel.WARNING,
    ]
    minor = [
        flags["has_published_at"] and date_source != Document.DateSource.PAGE,
        rubric_expected and not has_rubric,
        flags["text_short"] and not much_shorter,
        not flags["has_title"],
    ]
    if any(severe) or sum(major) >= 2:
        return 2
    if any(major):
        return 3
    if any(minor):
        return 4
    return 5


def open_incident_level(source) -> str:
    levels = set(source.incidents.filter(ended_at__isnull=True).values_list("level", flat=True))
    if DocumentQuality.IncidentLevel.CRITICAL in levels:
        return DocumentQuality.IncidentLevel.CRITICAL
    if DocumentQuality.IncidentLevel.WARNING in levels:
        return DocumentQuality.IncidentLevel.WARNING
    return DocumentQuality.IncidentLevel.NONE


def assess(document: Document, raw_html: str | None = None, incident_level: str | None = None):
    """Новая актуальная оценка документа (прежняя уходит в историю)."""
    if raw_html is None:
        latest = document.versions.order_by("-version").first()
        raw_html = latest.raw_html if latest else ""
    if incident_level is None:
        incident_level = open_incident_level(document.source)
    flags = compute_flags(document, raw_html, incident_level)
    return DocumentQuality.objects.create(
        document=document,
        score=_score_document(document, flags),
        quality_rules_version=RULES_VERSION,
        **flags,
    )


FLAG_FIELDS = (
    "has_title", "has_published_at", "date_plausible", "text_ok", "text_short", "text_empty",
    "lang_ok", "boilerplate_dup", "blocked_page", "selectors_ok", "extraction_ratio_ok",
)  # fmt: skip


def _score_document(document: Document, flags: dict) -> int:
    is_web = document.source.platform == Platform.WEB
    return score(
        flags,
        date_source=document.date_source,
        rubric_expected=is_web,
        has_rubric=bool(document.rubric),
        text_length=len(document.text.strip()),
        min_length=document.source.min_text_length,
        short_form=not is_web,
    )


def technical_score(quality: DocumentQuality) -> int:
    """Оценка по тем же флагам, но без учёта открытого инцидента источника."""
    flags = {name: getattr(quality, name) for name in FLAG_FIELDS}
    return _score_document(quality.document, {**flags, "incident_level": ""})
