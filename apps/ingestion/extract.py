"""Извлечение текста и метаданных статьи (README §4, шаг 2): trafilatura + htmldate.

Дата публикации берётся со страницы; если там её нет — из ленты, и это видно по date_source
(запасной источник снижает оценку качества, README §4.3). Временем сбора дата не подменяется.
"""

import datetime
import html as html_lib
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import lxml.html
from htmldate import find_date
from trafilatura import bare_extraction

from apps.documents.models import Document
from apps.documents.utils import normalize_url
from apps.ingestion.quality import detect_language

PRAGUE = ZoneInfo("Europe/Prague")
MAX_RUBRIC_NAME = 60


@dataclass
class Extracted:
    title: str
    text: str
    published_at: datetime.datetime | None
    date_source: str
    language: str
    categories: list[str]
    canonical_url: str
    rubric: str = ""


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


def _json_ld(root) -> list[dict]:
    """Все объекты JSON-LD страницы (включая вложенные @graph и списки)."""
    found = []
    for script in root.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                found.append(item)
                stack.extend(v for k, v in item.items() if k == "@graph")
    return found


def _names(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in values if isinstance(v, str | int) and str(v).strip()]


def _microdata_value(element, prop: str) -> str:
    found = element.xpath(f'.//*[@itemprop="{prop}"]')
    if not found:
        return ""
    node = found[0]
    return str(node.get("content") or node.get("href") or node.text_content() or "").strip()


def _breadcrumbs(root) -> list[list[tuple[str, str, float]]]:
    """Списки хлебных крошек (имя, ссылка, позиция): JSON-LD, затем микроразметка."""
    lists = []
    for obj in _json_ld(root):
        if obj.get("@type") != "BreadcrumbList":
            continue
        crumbs = []
        for item in obj.get("itemListElement") or []:
            if not isinstance(item, dict):
                continue
            target = item.get("item")
            name = item.get("name") or (target.get("name") if isinstance(target, dict) else "")
            link = (target.get("@id") or "") if isinstance(target, dict) else str(target or "")
            crumbs.append((str(name or ""), link, item.get("position") or 0))
        lists.append(crumbs)
    for element in root.xpath('//*[contains(@itemtype, "schema.org/BreadcrumbList")]'):
        crumbs = []
        for item in element.xpath('.//*[@itemprop="itemListElement"]'):
            try:
                position = float(_microdata_value(item, "position") or 0)
            except ValueError:
                position = 0
            name, link = _microdata_value(item, "name"), _microdata_value(item, "item")
            crumbs.append((name, link, position))
        lists.append(crumbs)
    return lists


GENERIC_SECTIONS = {"text", "article", "news", "novinka", "novinky", "zpravy", "zprávy"}


def page_rubric(html: str, title: str = "", url: str = "") -> str:
    """Рубрика со страницы: хлебные крошки (JSON-LD, микроразметка) → mrf:sections →
    articleSection → article:section.

    Нужна для лент без рубрик (Novinky, Seznam Zprávy): по ней отсекаются рубрики
    вне исследования, чтобы они не попадали в знаменатель (README §4, шаг 3; §5).
    Первая крошка (главная страница), сама статья и общие слова («Novinka») пропускаются.
    """
    try:
        root = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    for crumbs in _breadcrumbs(root):
        names = []
        for name, link, _ in sorted(crumbs, key=lambda c: c[2])[1:]:
            name = " ".join(html_lib.unescape(name).split())
            # Последняя крошка часто сама статья: её адрес — страница статьи.
            is_article = "/clanek/" in link or bool(
                url and link and normalize_url(link) == normalize_url(url)
            )
            if (
                is_article
                or len(name) > MAX_RUBRIC_NAME
                or name == title.strip()
                or name.lower() in GENERIC_SECTIONS
            ):
                continue
            if name and name not in names:
                names.append(name)
        if names:
            return " / ".join(names)[:200]
    meta = root.xpath('//meta[@property="mrf:sections"]/@content')
    if meta and meta[0].strip():
        return " / ".join(p.strip() for p in meta[0].split(";") if p.strip())[:200]
    for obj in _json_ld(root):
        if obj.get("articleSection"):
            return " / ".join(_names(obj["articleSection"]))[:200]
    sections = []
    for node in root.xpath('//*[@itemprop="articleSection"]'):
        name = " ".join(str(node.get("content") or node.text_content() or "").split())
        if name and name.lower() not in GENERIC_SECTIONS and name not in sections:
            sections.append(name)
    if sections:
        return " / ".join(sections)[:200]
    meta = root.xpath('//meta[@property="article:section"]/@content')
    return meta[0].strip()[:200] if meta else ""


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
        # Атрибут lang бывает шаблонным (42tcen.com: «en» у чешских статей) — текст надёжнее.
        language=detect_language(text) or _html_lang(html),
        categories=categories,
        canonical_url=canonical,
        rubric=page_rubric(html, title, url),
    )
