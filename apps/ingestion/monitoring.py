"""Мониторинг сбора: детекция тихих поломок (README §4.2).

Проверяется результат сбора, а не страница: объём, качество извлечения, средняя оценка
качества, HTTP/доступность и контрольные страницы. Каждая проверка пишет SourceHealthCheck
за день и отвечает за свои типы инцидентов: сигнал открывает (или продлевает) инцидент,
отсутствие сигнала закрывает его с сообщением «снова в норме». Проверка без достаточных
данных ничего не открывает и не закрывает.
"""

import datetime
import re
import statistics
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.actors.models import Platform, Source
from apps.documents.models import Document, DocumentQuality
from apps.panel import telegram
from apps.panel.defaults import get_setting
from apps.panel.models import Notification

from . import quality
from .extract import extract_article
from .http import PoliteClient
from .models import GoldenPage, Level, SourceHealthCheck, SourceIncident

Type = SourceIncident.Type
CheckType = SourceHealthCheck.CheckType

HISTORY_DAYS = 14
MIN_HISTORY_DAYS = 3  # меньше — нормы ещё нет
# Недельный ритм: профиль по дням недели из 3–4 недель; «ритм» — самый тихий день
# недели меньше половины самого активного (у медиа — выходные, у PSP — дни заседаний).
WEEKLY_HISTORY_DAYS = 28
WEEKLY_MIN_DAYS = 21
WEEKLY_RHYTHM = 0.5
MIN_DOCS = 5  # меньше документов за сутки — доли не считаются
SHARE_ALERT = 0.5
SHARE_JUMP = 0.3  # рост доли относительно нормы источника
HTTP_ERROR_SHARE = 0.3
MAX_EXAMPLES = 5


@dataclass
class Signal:
    incident_type: str
    level: str
    description: str
    examples: list = field(default_factory=list)


@dataclass
class CheckResult:
    check_type: str
    covers: set[str]  # типы инцидентов, о которых проверка может судить сейчас
    signals: list[Signal] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    details: str = ""

    @property
    def status(self) -> str:
        if any(s.level == Level.CRITICAL for s in self.signals):
            return Level.CRITICAL
        return Level.WARNING if self.signals else Level.OK


def _examples(docs) -> list[str]:
    return [d.url for d in docs[:MAX_EXAMPLES]]


# --- Проверки -----------------------------------------------------------------------------


def check_volume(source: Source, now) -> CheckResult:
    result = CheckResult(CheckType.VOLUME, set())
    docs = Document.objects.filter(source=source)
    first = docs.order_by("collected_at").values_list("collected_at", flat=True).first()
    last = docs.order_by("-collected_at").values_list("collected_at", flat=True).first()

    # Лента «молчит» дольше порога источника (у редких Telegram-каналов он больше).
    stale_after = datetime.timedelta(hours=source.stale_after_hours)
    reference = last or (
        source.collection_runs.order_by("started_at").values_list("started_at", flat=True).first()
    )
    if reference is not None:
        result.covers.add(Type.STALE_FEED)
        result.metrics["hours_since_last_document"] = round(
            (now - reference).total_seconds() / 3600, 1
        )
        if now - reference > stale_after:
            result.signals.append(
                Signal(
                    Type.STALE_FEED,
                    Level.WARNING,
                    f"Žádné nové dokumenty {result.metrics['hours_since_last_document']} h "
                    f"(práh {source.stale_after_hours} h).",
                )
            )

    if first is None:
        return result
    available = (now - first).days
    days = min(HISTORY_DAYS, available)
    if days < MIN_HISTORY_DAYS:
        result.details = f"Norma ještě není k dispozici: {days} dní historie."
        return result

    def count(start, end):
        return docs.filter(collected_at__gte=start, collected_at__lt=end).count()

    day = datetime.timedelta(days=1)
    today = count(now - day, now)
    # Окна по 24 ч назад; окно i·7 дней назад — тот же день недели, что и сегодняшнее.
    span = min(WEEKLY_HISTORY_DAYS, available)
    counts = {i: count(now - (i + 1) * day, now - i * day) for i in range(1, span + 1)}
    median = statistics.median([counts[i] for i in range(1, days + 1)])
    norm_kind = "den"
    if span >= WEEKLY_MIN_DAYS:
        # Выраженный недельный ритм (README §4.2): норма — медиана того же дня недели.
        profile = [statistics.median([counts[i] for i in counts if i % 7 == k]) for k in range(7)]
        if max(profile) > 0 and min(profile) < WEEKLY_RHYTHM * max(profile):
            median, norm_kind = profile[0], "stejný den v týdnu"
            result.metrics["weekday_profile"] = profile
    ratio = float(get_setting("monitoring-volume-ratio"))
    result.metrics.update(n=today, median=median, history_days=days, norm=norm_kind)
    if median <= 0:
        return result  # у источника нет ежедневного ритма (например, PSP между заседаниями)
    result.covers |= {Type.NO_DOCUMENTS, Type.LOW_VOLUME}
    if today == 0:
        result.signals.append(
            Signal(
                Type.NO_DOCUMENTS,
                Level.CRITICAL,
                f"0 dokumentů za den (norma {median}, {norm_kind}).",
            )
        )
    elif today < median * ratio:
        result.signals.append(
            Signal(
                Type.LOW_VOLUME,
                Level.WARNING,
                f"{today} dokumentů za den při normě {median} "
                f"({norm_kind}; práh {ratio:.0%} normy).",
            )
        )
    return result


def _shares(qualities) -> dict:
    stats = qualities.aggregate(
        n=Count("id"),
        no_date=Count("id", filter=Q(has_published_at=False)),
        no_title=Count("id", filter=Q(has_title=False)),
        short=Count("id", filter=Q(text_short=True) | Q(text_empty=True)),
        lang=Count("id", filter=Q(lang_ok=False)),
    )
    n = stats.pop("n")
    return {"n": n, **{k: (v / n if n else 0.0) for k, v in stats.items()}}


def check_extraction(source: Source, now) -> CheckResult:
    result = CheckResult(CheckType.EXTRACTION, set())
    current = DocumentQuality.objects.filter(is_current=True, document__source=source)
    recent = current.filter(document__collected_at__gte=now - datetime.timedelta(days=1))
    baseline = current.filter(
        document__collected_at__lt=now - datetime.timedelta(days=1),
        document__collected_at__gte=now - datetime.timedelta(days=HISTORY_DAYS + 1),
    )
    shares, norm = _shares(recent), _shares(baseline)
    result.metrics = {"n": shares["n"], **{k: round(v, 3) for k, v in shares.items() if k != "n"}}

    # Заглушки и капча видны и на малом числе документов.
    result.covers |= {Type.BOILERPLATE, Type.BLOCKED}
    for flag, incident_type, text in (
        ("boilerplate_dup", Type.BOILERPLATE, "stejný text u různých URL"),
        ("blocked_page", Type.BLOCKED, "captcha / antibot / souhlas / chyba místo článku"),
    ):
        bad = Document.objects.filter(
            pk__in=recent.filter(**{flag: True}).values("document_id")
        ).order_by("-collected_at")
        if len(bad[:2]) >= 2:
            result.signals.append(
                Signal(incident_type, Level.CRITICAL, f"{bad.count()} dok. za den: {text}.",
                       _examples(bad))
            )  # fmt: skip

    if shares["n"] < MIN_DOCS:
        result.details = f"Dokumentů za den je méně než {MIN_DOCS} — podíly se nehodnotí."
        return result
    result.covers |= {Type.EXTRACTION, Type.LANGUAGE}
    labels = {"no_date": "bez data", "no_title": "bez titulku", "short": "s krátkým textem"}
    problems = [
        f"{labels[k]} {shares[k]:.0%} (norma {norm[k]:.0%})"
        for k in labels
        if shares[k] >= SHARE_ALERT and (norm["n"] < MIN_DOCS or shares[k] >= norm[k] + SHARE_JUMP)
    ]
    if problems:
        flagged = recent.filter(
            Q(has_published_at=False) | Q(has_title=False) | Q(text_short=True) | Q(text_empty=True)
        ).select_related("document")
        result.signals.append(
            Signal(Type.EXTRACTION, Level.WARNING, "Dokumenty " + ", ".join(problems) + ".",
                   [q.document.url for q in flagged[:MAX_EXAMPLES]])
        )  # fmt: skip
    if shares["lang"] >= SHARE_ALERT:
        result.signals.append(
            Signal(
                Type.LANGUAGE,
                Level.WARNING,
                f"Jazyk neodpovídá očekávanému ({source.expected_language}) "
                f"u {shares['lang']:.0%} dokumentů.",
            )
        )
    return result


def check_quality(source: Source, now) -> CheckResult:
    """Пятая проверка (README §4.3): падение средней оценки качества источника."""
    result = CheckResult(CheckType.QUALITY, set())
    recent = DocumentQuality.objects.filter(
        is_current=True,
        document__source=source,
        document__collected_at__gte=now - datetime.timedelta(days=1),
    )
    # Оценка без штрафа за открытый инцидент: иначе любой инцидент сам порождал бы
    # «падение качества» и не давал ему закрыться.
    scores = [quality.technical_score(q) for q in recent.select_related("document__source")]
    n = len(scores)
    mean = sum(scores) / n if n else None
    result.metrics = {
        "n": n,
        "mean": round(mean, 2) if mean is not None else None,
        "distribution": {s: scores.count(s) for s in sorted(set(scores))},
    }
    if n < MIN_DOCS:
        return result
    result.covers.add(Type.QUALITY_DROP)
    threshold = float(get_setting("monitoring-min-mean-quality"))
    if mean < threshold:
        result.signals.append(
            Signal(
                Type.QUALITY_DROP,
                Level.WARNING,
                f"Průměrné hodnocení kvality za den {mean:.2f} (práh {threshold}).",
            )
        )
    return result


UNAVAILABLE = re.compile(r"není dostupný|vypnutý náhled")
# Исключения Python и БД — поломка у нас (код, миграции), а не у источника.
INTERNAL = re.compile(
    r"^(IntegrityError|ProgrammingError|OperationalError|DataError|InterfaceError|"
    r"AttributeError|TypeError|KeyError|NameError|ImportError|ModuleNotFoundError|"
    r"FieldError|FieldDoesNotExist)\b"
)


def check_http(source: Source, now) -> CheckResult:
    result = CheckResult(CheckType.HTTP, set())
    runs = list(source.collection_runs.filter(started_at__gte=now - datetime.timedelta(days=1)))
    finished = [r for r in runs if r.status != r.Status.RUNNING]
    if not finished:
        result.details = "Za den neproběhlo žádné spuštění sběru."
        return result
    result.covers |= {Type.HTTP_ERRORS, Type.CHANNEL_UNAVAILABLE, Type.INTERNAL_ERROR}

    requests = sum(r.http_requests for r in finished)
    codes: dict[str, int] = {}
    for run in finished:
        for code, n in (run.details.get("http") or {}).items():
            codes[code] = codes.get(code, 0) + n
    errors = sum(n for code, n in codes.items() if code[5:6] in {"4", "5"})

    def internal_run(run) -> bool:
        return any(INTERNAL.search(str(e)) for e in run.details.get("errors") or [])

    internal_runs = [r for r in finished if internal_run(r)]
    # Сигнал о нашей поломке — пока после неё не было чистого запуска: после исправления
    # инцидент закрывается с первым удачным запуском, а не через сутки.
    latest = max(finished, key=lambda r: r.started_at)
    # Spuštění padlá na naší chybě nejsou chybou zdroje: počítají se zvlášť.
    failed = [r for r in finished if r.status == r.Status.FAILED and r not in internal_runs]

    def total(key):
        return sum(int(r.details.get(key) or 0) for r in finished)

    timeouts, offsite, robots = (
        total("timeouts"),
        total("redirect_offsite"),
        total("robots_disallowed"),
    )
    result.metrics = {
        "runs": len(finished), "failed_runs": len(failed), "requests": requests, "codes": codes,
        "timeouts": timeouts, "redirect_offsite": offsite, "robots_disallowed": robots,
    }  # fmt: skip

    error_samples = [e for r in finished for e in r.details.get("errors", [])]
    internal = [e for e in error_samples if INTERNAL.search(str(e))]
    if internal and internal_run(latest):
        result.signals.append(
            Signal(
                Type.INTERNAL_ERROR, Level.CRITICAL,
                f"Sběr padá na interní chybě (kód nebo databáze), ne na zdroji: "
                f"{len(internal_runs)} z {len(finished)} spuštění. Po migraci zřejmě běží "
                "nerestartovaný worker: docker compose restart worker processor beat.",
                [str(e)[:300] for e in dict.fromkeys(internal)][:MAX_EXAMPLES],
            )
        )  # fmt: skip
    error_samples = [e for e in error_samples if e not in internal]
    unavailable = [e for e in error_samples if UNAVAILABLE.search(e)]
    if unavailable:
        result.signals.append(
            Signal(Type.CHANNEL_UNAVAILABLE, Level.CRITICAL,
                   "Kanál není dostupný, přejmenován nebo smazán.", sorted(set(unavailable))[:5])
        )  # fmt: skip

    problems = []
    if requests and errors / requests >= HTTP_ERROR_SHARE:
        problems.append(f"chyb HTTP {errors / requests:.0%} požadavků")
    if failed and len(failed) * 2 >= len(finished):
        problems.append(f"selhalo {len(failed)} z {len(finished)} spuštění")
    if timeouts >= 3:
        problems.append(f"časových limitů {timeouts}")
    if offsite >= 3:
        problems.append(f"přesměrování na jinou doménu {offsite}")
    if robots:
        problems.append(f"zakázáno v robots.txt: {robots}")
    broken_feeds = list(source.feeds.filter(is_active=True).exclude(last_error=""))
    if broken_feeds:
        problems.append(
            "kanály s chybou: " + ", ".join(f"{f.url} ({f.last_error})" for f in broken_feeds)
        )
    if problems:
        level = Level.CRITICAL if failed and len(failed) == len(finished) else Level.WARNING
        examples = [e for e in error_samples if e not in unavailable][:MAX_EXAMPLES]
        result.signals.append(Signal(Type.HTTP_ERRORS, level, "; ".join(problems) + ".", examples))
    return result


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def compare_golden(page: GoldenPage, html: str) -> list[str]:
    """Расхождения извлечения с эталоном; пустой список — совпадает."""
    extracted = extract_article(
        html, page.url, text_xpath=(page.source.collector_config or {}).get("text_xpath", "")
    )
    problems = []
    if _normalize(extracted.title) != _normalize(page.expected_title):
        problems.append(f"titulek „{extracted.title[:80]}»")
    prefix = _normalize(page.expected_text_prefix)[:150]
    # trafilatura иногда кладёт в начало текста заголовок — ищем эталон в первых символах.
    if prefix and prefix not in _normalize(extracted.text)[: len(prefix) + 300]:
        problems.append("začátek textu")
    if page.expected_published_at and (
        extracted.published_at is None
        or abs(extracted.published_at - page.expected_published_at) > datetime.timedelta(days=1)
    ):
        problems.append(f"datum {extracted.published_at}")
    return problems


def check_golden(source: Source, now, client: PoliteClient) -> CheckResult | None:
    pages = list(source.golden_pages.filter(is_active=True))
    if not pages:
        return None
    result = CheckResult(CheckType.GOLDEN, {Type.GOLDEN_MISMATCH})
    mismatches = []
    for page in pages:
        try:
            response = client.get(page.url)
            problems = (
                compare_golden(page, response.text) if response.ok else [f"HTTP {response.status}"]
            )
        except Exception as exc:  # noqa: BLE001 — недоступная страница тоже расхождение
            problems = [f"{type(exc).__name__}: {exc}"]
        page.last_checked_at = now
        page.last_status = Level.WARNING if problems else Level.OK
        page.save(update_fields=["last_checked_at", "last_status"])
        if problems:
            mismatches.append(f"{page.url}: " + ", ".join(problems))
    result.metrics = {"pages": len(pages), "mismatches": len(mismatches)}
    if mismatches:
        result.signals.append(
            Signal(Type.GOLDEN_MISMATCH, Level.WARNING,
                   "Extrakce neodpovídá vzoru — pravděpodobně se změnilo rozvržení stránky.",
                   mismatches[:MAX_EXAMPLES])
        )  # fmt: skip
    return result


# --- Инциденты и уведомления --------------------------------------------------------------


def _source_url(source: Source) -> str:
    return reverse("admin:actors_source_change", args=[source.pk])


def notify(source: Source, level: str, title: str, text: str, target) -> Notification:
    notification = Notification.objects.create(
        notification_type=Notification.Type.COLLECTION,
        level=level,
        title=title,
        text=text,
        url=_source_url(source),
        target=target,
    )
    transaction.on_commit(lambda: telegram.send(notification))
    return notification


def raise_signal(source: Source, signal: Signal, now) -> SourceIncident:
    """Открывает инцидент или продлевает открытый: повторные сигналы не спамят."""
    incident = SourceIncident.objects.filter(
        source=source, incident_type=signal.incident_type, ended_at__isnull=True
    ).first()
    label = SourceIncident.Type(signal.incident_type).label
    if incident is None:
        incident = SourceIncident.objects.create(
            source=source,
            incident_type=signal.incident_type,
            level=signal.level,
            started_at=now,
            description=signal.description,
            examples=signal.examples,
        )
        notify(source, signal.level, f"{source.name}: {label}",
               _with_examples(signal), incident)  # fmt: skip
        return incident
    escalated = signal.level == Level.CRITICAL and incident.level != Level.CRITICAL
    incident.occurrences += 1
    incident.description = signal.description
    incident.examples = signal.examples or incident.examples
    if escalated:
        incident.level = Level.CRITICAL
    incident.save(update_fields=["occurrences", "description", "examples", "level"])
    if escalated:
        notify(source, Level.CRITICAL, f"{source.name}: {label} — nyní kritické",
               _with_examples(signal), incident)  # fmt: skip
    return incident


def _with_examples(signal: Signal) -> str:
    examples = "\n".join(f"• {e}" for e in signal.examples[:3])
    return f"{signal.description}\n{examples}".strip()


def resolve(source: Source, incident_type: str, now) -> SourceIncident | None:
    incident = SourceIncident.objects.filter(
        source=source, incident_type=incident_type, ended_at__isnull=True
    ).first()
    if incident is None:
        return None
    incident.ended_at = now
    incident.save(update_fields=["ended_at"])
    from .tasks import recheck_incident

    transaction.on_commit(lambda: recheck_incident.delay(incident.pk))
    hours = (now - incident.started_at).total_seconds() / 3600
    notify(
        source,
        Notification.Level.INFO,
        f"{source.name}: opět v normě",
        f"{incident.get_incident_type_display()} — uzavřeno, trvalo {hours:.1f} h.",
        incident,
    )
    return incident


def apply(source: Source, result: CheckResult, now) -> None:
    with transaction.atomic():
        SourceHealthCheck.objects.update_or_create(
            source=source,
            date=timezone.localdate(now),
            check_type=result.check_type,
            defaults={"status": result.status, "metrics": result.metrics, "details": result.details
                      or "\n".join(s.description for s in result.signals)},
        )  # fmt: skip
        signalled = {s.incident_type for s in result.signals}
        for signal in result.signals:
            raise_signal(source, signal, now)
        for incident_type in result.covers - signalled:
            resolve(source, incident_type, now)


def monitor_source(source: Source, now=None) -> list[CheckResult]:
    now = now or timezone.now()
    results = [
        check(source, now) for check in (check_volume, check_extraction, check_quality, check_http)
    ]
    for result in results:
        apply(source, result, now)
    return results


def health(source: Source) -> str:
    """Статус здоровья для списка источников: green / yellow / red."""
    levels = set(source.incidents.filter(ended_at__isnull=True).values_list("level", flat=True))
    if Level.CRITICAL in levels:
        return "red"
    return "yellow" if levels else "green"


def golden_sources():
    return Source.objects.filter(
        is_active=True, platform=Platform.WEB, golden_pages__is_active=True
    ).distinct()
