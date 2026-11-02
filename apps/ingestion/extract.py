"""Извлечение текста и метаданных статьи (README §4, шаг 2): trafilatura + htmldate.

Дата публикации берётся со страницы; если там её нет — из ленты, и это видно по date_source
(запасной источник снижает оценку качества, README §4.3). Временем сбора дата не подменяется.
"""

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import lxml.html
from htmldate import find_date
from trafilatura import bare_extraction

from apps.documents.models import Document

PRAGUE = ZoneInfo("Europe/Prague")


@dataclass
class Extracted:
    title: str
    text: str
    published_at: datetime.datetime | None
    date_source: str
    language: str
    categories: list[str]
    canonical_url: str


def parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=PRAGUE)


def _html_lang(html: str) -> str:
    try:
        root = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    return (root.get("lang") or "").split("-")[0].lower()


def extract_article(html: str, url: str, feed_published_at=None) -> Extracted:
    doc = bare_extraction(
        html, url=url, with_metadata=True, include_comments=False, favor_precision=True
    )
    title = (getattr(doc, "title", None) or "").strip()
    text = (getattr(doc, "text", None) or "").strip()
    categories = list(getattr(doc, "categories", None) or [])
    canonical = getattr(doc, "url", None) or ""

    page_date = parse_dt(
        find_date(html, url=url, original_date=True, outputformat="%Y-%m-%dT%H:%M:%S%z")
    )
    if page_date is not None:
        # На странице часто только день — время берём из ленты, если день совпадает.
        midnight = page_date.timetz().replace(tzinfo=None) == datetime.time(0, 0)
        if (
            midnight
            and feed_published_at is not None
            and feed_published_at.astimezone(PRAGUE).date() == page_date.date()
        ):
            page_date = feed_published_at
        published_at, date_source = page_date, Document.DateSource.PAGE
    elif feed_published_at is not None:
        published_at, date_source = feed_published_at, Document.DateSource.FEED
    else:
        published_at, date_source = None, Document.DateSource.NONE

    return Extracted(
        title=title,
        text=text,
        published_at=published_at,
        date_source=date_source,
        language=_html_lang(html),
        categories=categories,
        canonical_url=canonical,
    )
