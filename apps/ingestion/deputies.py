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

from apps.actors.models import Actor, ActorAccount, ActorInclusionPeriod, Layer, Platform, Source
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


def _sync_periods(actor: Actor, deputy: psp.Deputy, term_code: str) -> None:
    existing = {p.start_date: p for p in actor.inclusion_periods.all()}
    for mandate in deputy.mandates:
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
                role=deputy.role,
                status=Actor.Status.ACTIVE if deputy.is_active else Actor.Status.EXCLUDED,
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
            _sync_periods(actor, deputy, term.code)

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
