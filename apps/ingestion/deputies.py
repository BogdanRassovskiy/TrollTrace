"""Синхронизация депутатов PSP с акторами (README §2 «Акторы»: институциональное определение).

Идемпотентно: актор находится по ActorAccount(источник PSP, id_osoba). Мандаты созыва
становятся периодами включения; конец мандата закрывает период и исключает актора (его
документы остаются). Все изменения пишутся в журнал аудита от имени импорта.
"""

import io
from dataclasses import dataclass, field

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps

from apps.actors.models import (
    Actor,
    ActorAccount,
    ActorInclusionPeriod,
    ActorPosition,
    Layer,
    Platform,
    Source,
)
from apps.panel import audit

from . import psp
from .http import PoliteClient

ICON_SIZE = 128  # px, квадрат; в интерфейсе показывается 32–64 px
NOTE = "import_psp_deputies"


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    photos: int = 0
    photo_errors: list[str] = field(default_factory=list)


def get_psp_source(term: psp.Term | None = None) -> Source:
    source, created = Source.objects.get_or_create(
        url=psp.SOURCE_URL,
        defaults=dict(
            name="Poslanecká sněmovna — stenoprotokoly",
            layer=Layer.PARLIAMENT,
            platform=Platform.PARLIAMENT,
            collect_interval_minutes=12 * 60,
            request_delay_seconds=1.5,
            min_text_length=50,
            stale_after_hours=24 * 21,  # между заседаниями бывают недели тишины
        ),
    )
    if term is not None and source.collector_config.get("term") != psp.term_slug(term.start):
        source.collector_config = {**source.collector_config, "term": psp.term_slug(term.start)}
        source.save(update_fields=["collector_config", "updated_at"])
    if created:
        audit.log_create(None, source, note=NOTE)
    return source


def make_icon(image_bytes: bytes) -> bytes:
    """Квадратная иконка: портрет обрезается по центру, чуть выше середины (лицо)."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        icon = ImageOps.fit(
            img.convert("RGB"),
            (ICON_SIZE, ICON_SIZE),
            Image.Resampling.LANCZOS,
            centering=(0.5, 0.3),
        )
        out = io.BytesIO()
        icon.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


def _download_photo(client: PoliteClient, deputy: psp.Deputy) -> tuple[str, bytes]:
    """Фото из каталога текущего созыва, иначе — из прошлых созывов депутата."""
    status = None
    for start in deputy.term_starts:
        url = psp.photo_url(start, deputy.id_osoba)
        response = client.get(url)
        if response.ok:
            return url, response.content
        status = response.status
    raise RuntimeError(f"HTTP {status}")


def _sync_positions(actor: Actor, deputy: psp.Deputy) -> None:
    """Мандаты — функции «poslanec PSP» рядом с другими функциями того же человека."""
    for mandate in deputy.mandates:
        position, created = ActorPosition.objects.get_or_create(
            actor=actor,
            kind=ActorPosition.Kind.DEPUTY,
            start_date=mandate.start,
            defaults={
                "title": deputy.role[:1].lower() + deputy.role[1:],
                "organization": "Poslanecká sněmovna",
                "end_date": mandate.end,
                "external_id": str(deputy.id_osoba),
                "source_url": psp.DETAIL_URL.format(id=deputy.id_osoba),
            },
        )
        title = deputy.role[:1].lower() + deputy.role[1:]
        if created:
            audit.log_create(None, position, note=NOTE)
        elif (position.end_date, position.title) != (mandate.end, title):
            before = audit.snapshot(position)
            position.end_date, position.title = mandate.end, title
            position.save(update_fields=["end_date", "title"])
            audit.log_change(None, position, before, note=NOTE)


def _sync_chamber_functions(actor: Actor, deputy: psp.Deputy) -> None:
    """Функции и членство в органах Sněmovny — информативно, для полноты базы.
    В сводку функций и в решение о выборке идёт только руководство Sněmovny."""
    for function in deputy.functions:
        kind = (
            ActorPosition.Kind.CHAMBER_LEADERSHIP
            if function.leadership
            else ActorPosition.Kind.CHAMBER
        )
        position, created = ActorPosition.objects.get_or_create(
            actor=actor,
            kind=kind,
            title=function.title[:200],
            start_date=function.start,
            defaults={
                "organization": function.organ[:200],
                "end_date": function.end,
                "external_id": str(deputy.id_osoba),
                "source_url": psp.DETAIL_URL.format(id=deputy.id_osoba),
            },
        )
        if created:
            audit.log_create(None, position, note=NOTE)
        elif position.end_date != function.end:
            before = audit.snapshot(position)
            position.end_date = function.end
            position.save(update_fields=["end_date"])
            audit.log_change(None, position, before, note=NOTE)


def _other_functions(actor: Actor) -> bool:
    """Держит актора в выборке после конца мандата: другая функция или решение исследователя."""
    return actor.manual_inclusion or (
        actor.positions.filter(end_date__isnull=True)
        .exclude(
            kind__in=[
                ActorPosition.Kind.DEPUTY,
                ActorPosition.Kind.CHAMBER,
                ActorPosition.Kind.CHAMBER_LEADERSHIP,
            ]
        )
        .exists()
    )


def _sync_periods(actor: Actor, deputy: psp.Deputy, term_code: str) -> None:
    existing = {p.start_date: p for p in actor.inclusion_periods.all()}
    # Мандат закончился, но человек остаётся, например, министром — период не закрываем.
    keep_open = _other_functions(actor)
    for mandate in deputy.mandates:
        if mandate.end and keep_open:
            mandate = psp.Mandate(mandate.start, None)
        period = existing.get(mandate.start)
        if period is None:
            period = ActorInclusionPeriod.objects.create(
                actor=actor,
                start_date=mandate.start,
                end_date=mandate.end,
                inclusion_reason=f"Poslanecký mandát PSP ({term_code})",
                exclusion_reason="Mandát zanikl (registr psp.cz)" if mandate.end else "",
            )
            audit.log_create(None, period, note=NOTE)
        elif period.end_date != mandate.end:
            before = audit.snapshot(period)
            period.end_date = mandate.end
            period.exclusion_reason = "Mandát zanikl (registr psp.cz)" if mandate.end else ""
            period.save()
            audit.log_change(None, period, before, note=NOTE)


def sync_deputies(
    client: PoliteClient, *, term_code: str | None = None, photos: bool = True, refresh_photos=False
) -> SyncResult:
    archive = client.get(psp.OPEN_DATA_URL)
    if not archive.ok:
        raise RuntimeError(f"Registr poslanců: HTTP {archive.status}")
    term = psp.parse_registry(archive.content, term_code)
    source = get_psp_source(term)
    result = SyncResult()

    for deputy in term.deputies:
        with transaction.atomic():
            account = (
                ActorAccount.objects.select_related("actor")
                .filter(source=source, account_id=str(deputy.id_osoba))
                .first()
            )
            fields = dict(
                name=deputy.name,
                actor_type=Actor.Type.POLITICIAN,
                layer=Layer.PARLIAMENT,
                party=deputy.club,
                status=Actor.Status.ACTIVE
                if deputy.is_active or (account is not None and _other_functions(account.actor))
                else Actor.Status.EXCLUDED,
                inclusion_criterion=f"Poslanec PSP, volební období {term.code} (registr psp.cz)",
                inclusion_reference=psp.DETAIL_URL.format(id=deputy.id_osoba),
            )
            if account is None:
                actor = Actor.objects.create(**fields)
                account = ActorAccount.objects.create(
                    actor=actor,
                    source=source,
                    account_id=str(deputy.id_osoba),
                    url=psp.DETAIL_URL.format(id=deputy.id_osoba),
                )
                audit.log_create(None, actor, note=NOTE)
                audit.log_create(None, account, note=NOTE)
                result.created += 1
            else:
                actor = account.actor
                before = audit.snapshot(actor)
                for name, value in fields.items():
                    setattr(actor, name, value)
                actor.save()
                changed = audit.log_change(None, actor, before, note=NOTE)
                result.updated += bool(changed)
                result.unchanged += not changed
            _sync_positions(actor, deputy)
            _sync_chamber_functions(actor, deputy)
            _sync_periods(actor, deputy, term.code)
            actor.refresh_role()

        if photos and (refresh_photos or not actor.photo):
            try:
                url, content = _download_photo(client, deputy)
                actor.photo.save(
                    f"psp-{deputy.id_osoba}.jpg", ContentFile(make_icon(content)), save=False
                )
                actor.photo_source_url = url
                actor.save(update_fields=["photo", "photo_source_url", "updated_at"])
                result.photos += 1
            except Exception as exc:  # noqa: BLE001 — без фото актор всё равно нужен
                result.photo_errors.append(f"{deputy.name}: {exc}")
    return result
