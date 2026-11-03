"""Сайты (слои A, B, D): RSS/Atom или sitemap → страница статьи → текст (trafilatura)."""

import calendar
import datetime
import xml.etree.ElementTree as ET

import feedparser
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.utils import normalize_url
from apps.ingestion.extract import extract_article, parse_dt
from apps.ingestion.http import RobotsDisallowed
from apps.ingestion.models import SourceFeed
from apps.ingestion.store import Item

from .base import Collector

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


class WebCollector(Collector):
    """Настройки (collector_config): max_articles — сколько новых статей за запуск (100),
    max_child_sitemaps — сколько свежих вложенных sitemap читать из индекса (2)."""

    def collect(self):
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
            feed.last_error = "Zakázáno v robots.txt"
            self.error(f"Kanál {feed.url}: zakázáno v robots.txt")
        except Exception as exc:  # noqa: BLE001 — одна лента не должна валить весь источник
            feed.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            self.error(f"Kanál {feed.url}: {feed.last_error}")
        feed.save()
        return entries

    def _read_sitemap(self, content: bytes, rubric: str) -> list[FeedEntry]:
        entries, children = parse_sitemap(content, rubric)
        for child in children[: int(self.config.get("max_child_sitemaps", 2))]:
            result = self.client.get(child)
            if result.ok:
                entries.extend(parse_sitemap(result.content, rubric)[0])
            else:
                self.error(f"Sitemap {child}: HTTP {result.status}")
        return entries

    def _collect_article(self, actor, entry: FeedEntry):
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
        html = result.text
        extracted = extract_article(html, result.final_url, feed_published_at=entry.published_at)
        self.save(
            actor,
            Item(
                url=entry.url,
                title=extracted.title or entry.title,
                text=extracted.text,
                raw_html=html,
                published_at=extracted.published_at,
                date_source=extracted.date_source,
                rubric=entry.rubric or ", ".join(extracted.categories),
                language=extracted.language,
                meta={"final_url": result.final_url} if result.final_url != entry.url else {},
            ),
        )
