"""Сайты (слои A, B, D, I): RSS/Atom, sitemap или страница-список → страница статьи →
парсер сайта (apps.ingestion.parsers, этап 3.5). Веб-источник без своего парсера не собирается."""

import calendar
import datetime
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

import feedparser
import lxml.html
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.utils import normalize_url
from apps.ingestion import parsers
from apps.ingestion.extract import parse_dt
from apps.ingestion.http import RobotsDisallowed
from apps.ingestion.models import SourceFeed
from apps.ingestion.store import Item

from .base import Collector

logger = logging.getLogger(__name__)

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


class FeedEntry:
    def __init__(self, url, title="", published_at=None, rubric=""):
        self.url, self.title, self.published_at, self.rubric = url, title, published_at, rubric


def parse_rss(content: bytes, rubric: str = "") -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    entries = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        struct = e.get("published_parsed") or e.get("updated_parsed")
        published = (
            datetime.datetime.fromtimestamp(calendar.timegm(struct), tz=datetime.UTC)
            if struct
            else None
        )
        tags = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        entries.append(FeedEntry(link, e.get("title", ""), published, rubric or ", ".join(tags)))
    return entries


def parse_sitemap(content: bytes, rubric: str = "") -> tuple[list[FeedEntry], list[str]]:
    """Возвращает (статьи, вложенные sitemap). Поддерживает news-sitemap и sitemap index."""
    root = ET.fromstring(content)
    tag = root.tag.split("}")[-1]
    if tag == "sitemapindex":
        children = [
            (
                el.findtext("sm:loc", "", SITEMAP_NS).strip(),
                el.findtext("sm:lastmod", "", SITEMAP_NS),
            )
            for el in root.findall("sm:sitemap", SITEMAP_NS)
        ]
        children.sort(key=lambda c: c[1] or "", reverse=True)
        return [], [loc for loc, _ in children if loc]
    entries = []
    for el in root.findall("sm:url", SITEMAP_NS):
        loc = el.findtext("sm:loc", "", SITEMAP_NS).strip()
        if not loc:
            continue
        date = el.findtext("news:news/news:publication_date", None, SITEMAP_NS) or el.findtext(
            "sm:lastmod", None, SITEMAP_NS
        )
        title = el.findtext("news:news/news:title", "", SITEMAP_NS)
        entries.append(FeedEntry(loc, title, parse_dt(date), rubric))
    return entries, []


def parse_listing(
    content: bytes, base_url: str, pattern: str = "", rubric: str = "", xpath: str = ""
) -> list[FeedEntry]:
    """Ссылки на статьи со страницы-списка: тот же сайт, (если задан) шаблон адреса
    и XPath блока со списком (когда у статей нет общего префикса в адресе, как у МВД)."""
    root = lxml.html.fromstring(content)
    host = urlsplit(base_url).netloc
    regex = re.compile(pattern) if pattern else None
    entries, seen = [], set()
    for href in root.xpath(xpath or "//a/@href"):
        href = str(href)
        url = urljoin(base_url, href.strip()).split("#")[0]
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.netloc != host:
            continue
        # Абсолютные ссылки http:// на https-сайте — лишний редирект на каждую статью.
        url = parts._replace(scheme=urlsplit(base_url).scheme).geturl()
        if regex is not None and not regex.search(parts.path):
            continue
        if url not in seen and url.rstrip("/") != base_url.rstrip("/"):
            seen.add(url)
            entries.append(FeedEntry(url, rubric=rubric))
    return entries


class WebCollector(Collector):
    """Настройки (collector_config): max_articles — сколько новых статей за запуск (100),
    max_child_sitemaps — сколько свежих вложенных sitemap читать из индекса (2).

    Всё, что зависит от вёрстки сайта (тело статьи, дата, автор, первоисточник, ссылки на
    странице-списке), задаёт парсер источника (Source.parser), а не настройки."""

    @property
    def parser(self):
        return parsers.parser_for(self.source)

    def notify_feed_disabled(self, feed: SourceFeed) -> None:
        from apps.ingestion.monitoring import notify
        from apps.panel.models import Notification

        notify(
            self.source,
            Notification.Level.WARNING,
            f"{self.source}: kanál vypnut kvůli robots.txt",
            f"Kanál {feed.url} je zakázán v robots.txt. Sběr ho už nezkouší; pokud zdroj "
            "nabízí povolený kanál (sitemap, jiná rubrika), přidejte ho v administraci zdroje.",
            feed,
        )

    def collect(self):
        if error := self.source.parser_error():
            self.error(error)
            logger.error("%s: %s", self.source, error)
            return
        account = (
            self.active_accounts().filter(account_id="").first() or self.active_accounts().first()
        )
        if account is None:
            self.error("Zdroj nemá aktivního aktéra (ActorAccount) — není komu sběr přiřadit.")
            return
        actor = account.actor

        entries: dict[str, FeedEntry] = {}
        for feed in self.source.feeds.filter(is_active=True):
            for entry in self._read_feed(feed):
                entries.setdefault(normalize_url(entry.url), entry)
        self.stats["found"] = len(entries)

        known = set(
            Document.objects.filter(source=self.source, url_normalized__in=entries).values_list(
                "url_normalized", flat=True
            )
        )
        fresh = [e for key, e in entries.items() if key not in known]
        fresh.sort(key=lambda e: e.published_at or timezone.now(), reverse=True)
        limit = int(self.config.get("max_articles", 100))
        self.stats["skipped"] += max(0, len(fresh) - limit) + len(known)

        for entry in fresh[:limit]:
            self._collect_article(actor, entry)

    def _read_feed(self, feed: SourceFeed) -> list[FeedEntry]:
        feed.last_fetched_at = timezone.now()
        headers = {}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified
        entries: list[FeedEntry] = []
        try:
            result = self.client.get(feed.url, headers=headers)
            if result.not_modified:
                feed.last_success_at, feed.last_error = feed.last_fetched_at, ""
            elif not result.ok:
                feed.last_error = f"HTTP {result.status}"
                self.error(f"Kanál {feed.url}: HTTP {result.status}")
            else:
                if feed.kind == SourceFeed.Kind.SITEMAP:
                    entries = self._read_sitemap(result.content, feed.rubric)
                elif feed.kind == SourceFeed.Kind.LISTING:
                    entries = parse_listing(
                        result.content,
                        result.final_url,
                        self.parser.listing_pattern,
                        feed.rubric,
                        self.parser.listing_xpath,
                    )
                else:
                    entries = parse_rss(result.content, feed.rubric)
                feed.etag = result.headers.get("etag", "")[:300]
                feed.last_modified = result.headers.get("last-modified", "")[:100]
                feed.last_success_at, feed.last_item_count, feed.last_error = (
                    feed.last_fetched_at,
                    len(entries),
                    "",
                )
                if not entries:
                    feed.last_error = "Kanál je prázdný"
        except RobotsDisallowed:
            # Запрет уважаем и больше не пробуем: лента выключается, исследователь получает
            # уведомление и может подключить разрешённую альтернативу (sitemap, другую рубрику).
            feed.is_active = False
            today = timezone.localdate()
            feed.last_error = (
                f"Zakázáno v robots.txt — kanál vypnut {today.day}. {today.month}. {today.year}"
            )
            self.error(f"Kanál {feed.url}: zakázáno v robots.txt, kanál vypnut")
            self.notify_feed_disabled(feed)
        except Exception as exc:  # noqa: BLE001 — одна лента не должна валить весь источник
            feed.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            self.error(f"Kanál {feed.url}: {feed.last_error}")
        feed.save()
        return entries

    def _read_sitemap(self, content: bytes, rubric: str) -> list[FeedEntry]:
        entries, children = parse_sitemap(content, rubric)
        for child in children[: int(self.config.get("max_child_sitemaps", 2))]:
            try:
                result = self.client.get(child)
            except RobotsDisallowed:
                self.error(f"Sitemap {child}: zakázáno v robots.txt")
                continue
            if result.ok:
                entries.extend(parse_sitemap(result.content, rubric)[0])
            else:
                self.error(f"Sitemap {child}: HTTP {result.status}")
        return entries

    def _collect_article(self, actor, entry: FeedEntry):
        if self.parser is None:
            self.error(self.source.parser_error() or "Zdroj nemá parser.")
            return
        try:
            result = self.client.get(entry.url)
        except RobotsDisallowed:
            self.stats["skipped"] += 1
            return
        except Exception as exc:  # noqa: BLE001
            self.error(f"{entry.url}: {type(exc).__name__}: {exc}")
            return
        if not result.ok:
            self.error(f"{entry.url}: HTTP {result.status}")
            return
        if self.parser.gated(result.final_url):
            self.error(
                f"{entry.url}: web přesměroval na {result.final_url} místo článku "
                "(souhlas s cookies / paywall) — obnovte cookies zdroje"
            )
            return
        html = result.text
        extracted = self.parser.parse(html, result.final_url, entry.published_at)
        if extracted.missing:
            self.stats["selectors_missing"] += 1
        meta = {"final_url": result.final_url} if result.final_url != entry.url else {}
        meta = parsers.merge_meta(meta, extracted)
        self.save(
            actor,
            Item(
                url=entry.url,
                title=extracted.title or entry.title,
                text=extracted.text,
                raw_html=html,
                published_at=extracted.published_at,
                date_source=extracted.date_source,
                rubric=extracted.rubric or entry.rubric or ", ".join(extracted.categories),
                language=extracted.language,
                meta=meta,
            ),
        )
