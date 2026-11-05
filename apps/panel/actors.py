"""Управление акторами из панели (README §7.2): добавление, импорт CSV, архивация, периоды.

Удаления нет: «удалить» = исключить (закрыть период включения). Документы, метки и история
остаются, сбор новых данных по исключённому актору останавливается (Collector.active_accounts),
на графиках он по умолчанию скрыт. Каждое действие пишется в журнал аудита.
"""

import csv
import datetime
import io
from dataclasses import dataclass, field

import httpx
from django.db import transaction
from django.db.models import Q

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.ingestion import parsers
from apps.ingestion.http import PoliteClient, RobotsDisallowed

from . import audit
from .models import AuditLog, Job

# Источники, где ретроспективный сбор по одному актору бессмыслен: стенограммы PSP собираются
# целиком, выступления распределяются по депутатам сами.
NO_RETRO_PLATFORMS = {Platform.PARLIAMENT}


# --- проверки ------------------------------------------------------------------------------


def _url_variants(url: str) -> list[str]:
    url = url.strip()
    base = url.rstrip("/")
    return list({url, base, base + "/"})


def find_source(url: str) -> Source | None:
    query = Q()
    for variant in _url_variants(url):
        query |= Q(url__iexact=variant)
    return Source.objects.filter(query).first()


def clean_handle(account_id: str) -> str:
    return account_id.strip().removeprefix("@")


def account_conflict(source: Source, account_id: str, url: str = "", exclude_actor=None):
    """Аккаунт или URL уже привязан к другому актору → возвращает этот ActorAccount."""
    query = Q(source=source, account_id__iexact=clean_handle(account_id))
    if url:
        for variant in _url_variants(url):
            query |= Q(url__iexact=variant)
    accounts = ActorAccount.objects.filter(query).select_related("actor", "source")
    if exclude_actor is not None:
        accounts = accounts.exclude(actor=exclude_actor)
    return accounts.first()


def check_available(url: str, client: PoliteClient | None = None) -> tuple[bool, str]:
    """Проверка доступности источника при добавлении. Уважает robots.txt, честный user-agent."""
    own = client is None
    client = client or PoliteClient(delay=0, timeout=10)
    try:
        result = client.get(url)
    except RobotsDisallowed:
        return False, "robots.txt sběr této adresy zakazuje"
    except httpx.HTTPError as exc:
        return False, f"zdroj je nedostupný ({type(exc).__name__})"
    finally:
        if own:
            client.close()
    if not result.ok:
        return False, f"zdroj odpověděl HTTP {result.status}"
    return True, f"zdroj odpověděl HTTP {result.status}"


@dataclass
class Consequences:
    """Предпросмотр последствий исключения: что затронуто и что останется."""

    documents: int
    labels: int
    open_period: object
    active_accounts: list
    sources_stopped: list


def consequences(actor: Actor) -> Consequences:
    from apps.documents.models import Document
    from apps.narratives.models import Label

    accounts = list(actor.accounts.select_related("source"))
    stopped = []
    for account in accounts:
        others = (
            ActorAccount.objects.filter(source=account.source, actor__status=Actor.Status.ACTIVE)
            .exclude(actor=actor)
            .exists()
        )
        if not others and account.source.is_active:
            stopped.append(account.source)
    return Consequences(
        documents=Document.objects.filter(actor=actor).count(),
        labels=Label.objects.filter(document__actor=actor).count(),
        open_period=actor.open_period,
        active_accounts=accounts,
        sources_stopped=stopped,
    )


# --- фоновые задачи ------------------------------------------------------------------------


def queue_aggregates(user, reason: str) -> Job:
    """Полный пересчёт агрегатов: периоды включения меняют знаменатели задним числом."""
    from apps.analytics.tasks import rebuild_aggregates

    job = Job.objects.create(
        job_type=Job.Type.AGGREGATE, params={"full": True, "reason": reason}, created_by=user
    )
    audit.log(None, AuditLog.Action.RUN, job, note=reason, user=user)
    transaction.on_commit(lambda: _remember_task(job, rebuild_aggregates.delay(job.pk)))
    return job


def queue_retro_collect(user, actor: Actor, sources) -> Job | None:
    from .tasks import retro_collect

    ids = [s.pk for s in sources if s.is_active and s.platform not in NO_RETRO_PLATFORMS]
    if not ids:
        return None
    job = Job.objects.create(
        job_type=Job.Type.RETRO_COLLECT,
        params={"actor": actor.pk, "sources": ids},
        created_by=user,
    )
    audit.log(None, AuditLog.Action.RUN, job, note=f"aktér {actor}", user=user)
    transaction.on_commit(lambda: _remember_task(job, retro_collect.delay(job.pk)))
    return job


def _remember_task(job: Job, result) -> None:
    Job.objects.filter(pk=job.pk).update(celery_task_id=getattr(result, "id", "") or "")


# --- действия ------------------------------------------------------------------------------


@dataclass
class NewSource:
    url: str
    name: str
    platform: str
    layer: str


@dataclass
class ActorDraft:
    """Данные нового актора: из формы или строки CSV."""

    name: str
    actor_type: str
    layer: str = ""
    party: str = ""
    is_hybrid: bool = False
    inclusion_criterion: str = ""
    inclusion_reference: str = ""
    start_date: datetime.date = field(default_factory=datetime.date.today)
    source: Source | None = None
    new_source: NewSource | None = None
    account_id: str = ""
    account_url: str = ""


@dataclass
class Created:
    actor: Actor
    jobs: list = field(default_factory=list)


def create_actor(request, draft: ActorDraft, *, note: str = "", queue: bool = True) -> Created:
    """Актор + период включения + источник/аккаунт; ставит ретросбор и пересчёт агрегатов."""
    user = request.user if request is not None and request.user.is_authenticated else None
    with transaction.atomic():
        actor = Actor.objects.create(
            name=draft.name,
            actor_type=draft.actor_type,
            layer=draft.layer,
            party=draft.party,
            is_hybrid=draft.is_hybrid,
            inclusion_criterion=draft.inclusion_criterion,
            inclusion_reference=draft.inclusion_reference,
            manual_inclusion=True,
        )
        audit.log_create(request, actor, note=note)
        period = actor.include(reason=draft.inclusion_criterion, on=draft.start_date)
        audit.log_create(request, period, note=note)
        source = draft.source
        new_sources = []
        if draft.new_source is not None:
            parser = ""
            if draft.new_source.platform == Platform.WEB:
                guessed = parsers.guess(draft.new_source.url)
                parser = guessed.key if guessed else ""
            source = Source.objects.create(
                url=draft.new_source.url,
                name=draft.new_source.name or draft.name,
                platform=draft.new_source.platform,
                layer=draft.new_source.layer or draft.layer,
                parser=parser,
                # веб без своего парсера не собирается (этап 3.5): сбор включат после парсера
                is_active=draft.new_source.platform != Platform.WEB or bool(parser),
            )
            if source.is_active:
                new_sources.append(source)
            audit.log_create(request, source, note=note)
        if source is not None:
            account = ActorAccount.objects.create(
                actor=actor, source=source, account_id=draft.account_id, url=draft.account_url
            )
            audit.log_create(request, account, note=note)
        result = Created(actor=actor)
        if queue:
            retro = queue_retro_collect(user, actor, new_sources)
            result.jobs = [j for j in (retro, queue_aggregates(user, f"nový aktér {actor}")) if j]
    return result


def exclude_actor(request, actor: Actor, reason: str, on: datetime.date | None = None) -> Job:
    user = request.user if request is not None else None
    with transaction.atomic():
        before_actor = audit.stored_snapshot(actor)
        period = actor.open_period
        before_period = audit.stored_snapshot(period) if period else {}
        actor.exclude(reason, on=on)
        audit.log(
            request,
            AuditLog.Action.ARCHIVE,
            actor,
            changes=audit.diff(before_actor, audit.snapshot(actor)),
            note=f"Vyřazení: {reason}",
        )
        if period is not None:
            period.refresh_from_db()
            audit.log_change(request, period, before_period, note=f"Vyřazení: {reason}")
        return queue_aggregates(user, f"vyřazen aktér {actor}")


def include_actor(request, actor: Actor, reason: str, on: datetime.date) -> list[Job]:
    """Повторное включение открывает новый период, старые не затираются."""
    user = request.user if request is not None else None
    with transaction.atomic():
        before = audit.stored_snapshot(actor)
        period = actor.include(reason=reason, on=on)
        audit.log(
            request,
            AuditLog.Action.RESTORE,
            actor,
            changes=audit.diff(before, audit.snapshot(actor)),
            note=f"Opětovné zařazení: {reason}",
        )
        audit.log_create(request, period, note=f"Opětovné zařazení: {reason}")
        sources = [a.source for a in actor.accounts.select_related("source")]
        jobs = [
            queue_retro_collect(user, actor, sources),
            queue_aggregates(user, f"znovu zařazen aktér {actor}"),
        ]
    return [j for j in jobs if j]


def last_period_end(actor: Actor) -> datetime.date | None:
    last = actor.inclusion_periods.order_by("-start_date").first()
    if last is None:
        return None
    return last.end_date or datetime.date.max


# --- импорт CSV ----------------------------------------------------------------------------

CSV_COLUMNS = [
    "name",
    "actor_type",
    "layer",
    "party",
    "is_hybrid",
    "source_url",
    "account_id",
    "account_url",
    "inclusion_criterion",
    "inclusion_reference",
    "start_date",
]
CSV_REQUIRED = {"name", "actor_type"}
TRUE = {"1", "ano", "true", "yes", "x", "a"}


@dataclass
class ImportRow:
    line: int
    values: dict
    errors: list = field(default_factory=list)
    draft: ActorDraft | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _sniff(text: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        return csv.excel


def decode_csv(content: bytes) -> str:
    """UTF-8 (в том числе из Excel с BOM), иначе Windows-1250 — частая кодировка чешских CSV."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1250", errors="replace")


def parse_csv(text: str) -> tuple[list[ImportRow], list[str]]:
    """Разбор и проверка без записи в БД. Возвращает строки и общие ошибки файла."""
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff(text))
    header = [h.strip() for h in reader.fieldnames or []]
    missing = CSV_REQUIRED - set(header)
    if missing:
        return [], [f"Chybí povinné sloupce: {', '.join(sorted(missing))}."]
    unknown = set(header) - set(CSV_COLUMNS)
    problems = [f"Neznámé sloupce se ignorují: {', '.join(sorted(unknown))}."] if unknown else []
    rows, seen_names, seen_accounts = [], set(), set()
    types, layers = set(Actor.Type.values), set(Layer.values)
    for number, raw in enumerate(reader, start=2):
        values = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(values.values()):
            continue
        row = ImportRow(line=number, values=values)
        rows.append(row)
        name, kind, layer = (
            values.get("name", ""),
            values.get("actor_type", ""),
            values.get("layer", ""),
        )
        if not name:
            row.errors.append("chybí jméno")
        if kind not in types:
            row.errors.append(f"neznámý typ „{kind}“ (povoleno: {', '.join(sorted(types))})")
        if layer and layer not in layers:
            row.errors.append(f"neznámá vrstva „{layer}“")
        key = (name.lower(), values.get("party", "").lower())
        if key in seen_names:
            row.errors.append("duplicitní řádek v souboru")
        seen_names.add(key)
        if Actor.objects.filter(name__iexact=name, party__iexact=values.get("party", "")).exists():
            row.errors.append("aktér se stejným jménem a stranou už existuje")
        start = datetime.date.today()
        if values.get("start_date"):
            try:
                start = datetime.date.fromisoformat(values["start_date"])
            except ValueError:
                row.errors.append("datum začátku musí být ve tvaru RRRR-MM-DD")
        source = None
        handle = clean_handle(values.get("account_id", ""))
        if values.get("source_url"):
            source = find_source(values["source_url"])
            if source is None:
                row.errors.append("zdroj s touto adresou neexistuje (zdroje přidejte formulářem)")
            else:
                conflict = account_conflict(source, handle, values.get("account_url", ""))
                if conflict is not None:
                    row.errors.append(f"účet už patří aktérovi „{conflict.actor}“")
                if (source.pk, handle.lower()) in seen_accounts:
                    row.errors.append("stejný účet je v souboru dvakrát")
                seen_accounts.add((source.pk, handle.lower()))
        if row.ok:
            row.draft = ActorDraft(
                name=name,
                actor_type=kind,
                layer=layer,
                party=values.get("party", ""),
                is_hybrid=values.get("is_hybrid", "").lower() in TRUE,
                inclusion_criterion=values.get("inclusion_criterion", ""),
                inclusion_reference=values.get("inclusion_reference", ""),
                start_date=start,
                source=source,
                account_id=handle,
                account_url=values.get("account_url", ""),
            )
    return rows, problems


def import_rows(request, rows: list[ImportRow], filename: str) -> list[Actor]:
    """Записывает только строки без ошибок; один пересчёт агрегатов на весь импорт."""
    user = request.user if request is not None else None
    note = f"Import CSV: {filename}"
    with transaction.atomic():
        actors = [
            create_actor(request, row.draft, note=note, queue=False).actor for row in rows if row.ok
        ]
        if actors:
            queue_aggregates(user, f"import {len(actors)} aktérů")
    return actors
