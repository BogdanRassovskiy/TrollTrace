"""Telegram (слой C): публичные каналы через веб-превью t.me/s/<канал>.

Без логина и API-ключей — только то, что Telegram показывает любому посетителю (README §6:
только публичное). Аккаунт актора (ActorAccount.account_id) — handle канала.

Настройки (collector_config): backfill_pages — сколько страниц истории читать при первом
сборе канала (3), max_pages — предел страниц за запуск при догоне пропуска (10).
"""

import datetime
import re

import lxml.html
from lxml import etree

from apps.documents.models import Document
from apps.ingestion.store import Item

from .base import Collector

BASE = "https://t.me"
MESSAGE_XPATH = (
    '//div[contains(concat(" ", normalize-space(@class), " "), " tgme_widget_message ")]'
    "[@data-post]"
)


def parse_count(value: str) -> int | None:
    """«1.2K» → 1200, «3M» → 3000000."""
    value = (value or "").strip().upper().replace(",", ".")
    match = re.fullmatch(r"([\d.]+)([KM]?)", value)
    if not match:
        return None
    number = float(match.group(1)) * {"": 1, "K": 1_000, "M": 1_000_000}[match.group(2)]
    return int(number)


def _text_with_breaks(node) -> str:
    for br in node.iter("br"):
        br.tail = "\n" + (br.tail or "")
    return node.text_content().strip()


def parse_channel_page(html: str) -> list[dict]:
    root = lxml.html.fromstring(html)
    messages = []
    for node in root.xpath(MESSAGE_XPATH):
        post = node.get("data-post")  # "channel/123"
        channel, _, msg_id = post.rpartition("/")
        if not msg_id.isdigit():
            continue
        text_nodes = node.xpath('.//div[contains(@class, "tgme_widget_message_text")]')
        time_nodes = node.xpath(
            './/a[contains(@class, "tgme_widget_message_date")]//time/@datetime'
        )
        views = node.xpath('string(.//span[contains(@class, "tgme_widget_message_views")])')
        forwarded = node.xpath('.//a[contains(@class, "tgme_widget_message_forwarded_from_name")]')
        messages.append(
            {
                "channel": channel,
                "id": int(msg_id),
                "text": _text_with_breaks(text_nodes[0]) if text_nodes else "",
                "published_at": datetime.datetime.fromisoformat(time_nodes[0])
                if time_nodes
                else None,
                "views": parse_count(views),
                "forwarded_from": (
                    {
                        "name": forwarded[0].text_content().strip(),
                        "url": forwarded[0].get("href", ""),
                    }
                    if forwarded
                    else None
                ),
                "media": sorted(
                    kind
                    for kind in ("photo", "video", "document", "poll")
                    if node.xpath(f'.//*[contains(@class, "tgme_widget_message_{kind}")]')
                ),
                "raw_html": etree.tostring(node, encoding="unicode"),
            }
        )
    return messages


class TelegramCollector(Collector):
    def collect(self):
        for account in self.active_accounts().exclude(account_id=""):
            try:
                self._collect_channel(account)
            except Exception as exc:  # noqa: BLE001 — один канал не валит остальные
                self.error(f"@{account.account_id}: {type(exc).__name__}: {exc}")

    def _known_max_id(self, handle: str) -> int:
        ids = Document.objects.filter(
            source=self.source, external_id__istartswith=f"{handle}/"
        ).values_list("external_id", flat=True)
        return max((int(i.rpartition("/")[2]) for i in ids), default=0)

    def _collect_channel(self, account):
        handle = account.account_id
        known_max = self._known_max_id(handle)
        max_pages = int(
            self.config.get("max_pages", 10) if known_max else self.config.get("backfill_pages", 3)
        )
        url = f"{BASE}/s/{handle}"
        for _page in range(max_pages):
            result = self.client.get(url)
            if not result.ok:
                self.error(f"@{handle}: HTTP {result.status}")
                return
            if "/s/" not in result.final_url:
                # Telegram отправляет на t.me/<канал>, если канала нет или превью выключено.
                self.error(f"@{handle}: kanál není dostupný nebo má vypnutý náhled")
                return
            messages = parse_channel_page(result.text)
            if not messages:
                return
            self.stats["found"] += len(messages)
            for msg in messages:
                self._save_message(account.actor, msg)
            oldest = min(m["id"] for m in messages)
            if oldest <= known_max + 1 or oldest <= 1:
                return  # дошли до уже собранного
            url = f"{BASE}/s/{handle}?before={oldest}"

    def _save_message(self, actor, msg):
        text = msg["text"]
        first_line = text.split("\n", 1)[0]
        self.save(
            actor,
            Item(
                url=f"{BASE}/{msg['channel']}/{msg['id']}",
                external_id=f"{msg['channel']}/{msg['id']}",
                title=first_line[:200],
                text=text,
                raw_html=msg["raw_html"],
                published_at=msg["published_at"],
                date_source=(
                    Document.DateSource.PAGE if msg["published_at"] else Document.DateSource.NONE
                ),
                engagement={"views": msg["views"]} if msg["views"] is not None else {},
                meta={
                    k: v
                    for k, v in (("forwarded_from", msg["forwarded_from"]), ("media", msg["media"]))
                    if v
                },
            ),
        )
