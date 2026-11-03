"""Стенограммы Poslanecké sněmovny (слой E): одно выступление = один документ.

Обход: оглавление созыва → дни заседаний (от новых к старым) → страницы стенограммы
(s001002.htm, ~10 минут заседания) → выступления. Уже обработанные страницы пропускаются,
поэтому догон архива созыва идёт порциями по max_pages страниц за запуск.

Оратор определяется по id_osoba из ссылки; неизвестные ораторы (члены правительства не из
депутатов, гости) заводятся автоматически с пометкой в основании включения.
"""

import datetime

from apps.actors.models import Actor, ActorAccount, Layer
from apps.documents.models import Document
from apps.ingestion import psp
from apps.ingestion.extract import PRAGUE
from apps.ingestion.store import Item
from apps.panel import audit

from .base import Collector


class PspCollector(Collector):
    """Настройки (collector_config): term — каталог созыва («2025ps»), max_pages — сколько
    страниц стенограммы обработать за запуск (150)."""

    def collect(self):
        term = self.config.get("term", "2025ps")
        base = f"{psp.PSP}/eknih/{term}/stenprot/"
        budget = int(self.config.get("max_pages", 150))

        index = self.client.get(base + "index.htm")
        if not index.ok:
            self.error(f"Obsah {base}: HTTP {index.status}")
            return
        done_pages = set(
            Document.objects.filter(source=self.source)
            .values_list("meta__page", flat=True)
            .distinct()
        )
        self._actors = {
            a.account_id: a.actor
            for a in ActorAccount.objects.filter(source=self.source).select_related("actor")
        }

        for meeting, day in psp.day_pages(index.text):
            if budget <= 0:
                break
            day_url = f"{base}{meeting}/{day}.html"
            result = self.client.get(day_url)
            if not result.ok:
                self.error(f"{day_url}: HTTP {result.status}")
                continue
            for page in psp.speech_pages(result.text):
                page_url = f"{base}{meeting}/{page}"
                if page_url in done_pages:
                    continue
                if budget <= 0:
                    break
                budget -= 1
                self._collect_page(term, meeting, page_url)
                done_pages.add(page_url)

    def _collect_page(self, term: str, meeting: str, page_url: str):
        result = self.client.get(page_url)
        if not result.ok:
            self.error(f"{page_url}: HTTP {result.status}")
            return
        page = psp.parse_steno_page(result.text)
        self.stats["found"] += len(page.speeches)
        published = (
            datetime.datetime.combine(page.date, page.time or datetime.time(0, 0), tzinfo=PRAGUE)
            if page.date
            else None
        )
        meeting_no = int(meeting[:3])
        for speech in page.speeches:
            actor = self._resolve_actor(speech, page.date)
            if actor is None:
                self.stats["skipped"] += 1
                continue
            if actor.status != Actor.Status.ACTIVE:
                self.stats["skipped"] += 1  # исключённый актор: новые данные не собираем
                continue
            page_name = page_url.rsplit("/", 1)[1]
            self.save(
                actor,
                Item(
                    url=f"{page_url}#{speech.anchor}",
                    external_id=f"{term}/{meeting}/{page_name}#{speech.anchor}",
                    title=f"{meeting_no}. schůze, {page.date or '?'}: {speech.speaker_label}",
                    text=speech.text,
                    raw_html=result.text,
                    published_at=published,
                    date_source=Document.DateSource.PAGE if published else Document.DateSource.NONE,
                    language="cs",
                    meta={
                        "page": page_url,
                        "meeting": meeting_no,
                        "speaker_label": speech.speaker_label,
                        "psp_id": speech.speaker_id,
                    },
                ),
            )

    def _resolve_actor(self, speech: psp.Speech, date) -> Actor | None:
        if speech.speaker_id is None:
            # Продолжение выступления с прошлой страницы: «(pokračuje Věra Kovářová)».
            name = psp.name_from_label(speech.speaker_label)
            return next((a for a in self._actors.values() if a.name == name), None)
        key = str(speech.speaker_id)
        if key not in self._actors:
            actor = Actor.objects.create(
                name=psp.name_from_label(speech.speaker_label),
                actor_type=Actor.Type.POLITICIAN,
                layer=Layer.PARLIAMENT,
                role=" ".join(speech.speaker_label.split()[:-2])[:100],
                inclusion_criterion="Automaticky: vystoupení v Poslanecké sněmovně "
                "(není v registru poslanců — zkontrolovat)",
                inclusion_reference=psp.DETAIL_URL.format(id=key),
            )
            account = ActorAccount.objects.create(
                actor=actor, source=self.source, account_id=key, url=psp.DETAIL_URL.format(id=key)
            )
            actor.include("První vystoupení ve stenoprotokolu", on=date or datetime.date.today())
            audit.log_create(None, actor, note="Sběr stenoprotokolů PSP")
            audit.log_create(None, account, note="Sběr stenoprotokolů PSP")
            self._actors[key] = actor
        return self._actors[key]
