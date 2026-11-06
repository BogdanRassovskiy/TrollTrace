"""Парсер одного сайта: явные селекторы вместо угадывания (этап 3.5, README §4).

Общий экстрактор (trafilatura) может молча взять не тот блок — анонсы, обсуждение, соседнюю
статью, — и сбор формально проходит. У парсера сайта каждое поле задано селектором; если
обязательный селектор ничего не нашёл, это видно сразу: поле попадает в `missing`,
документ получает selectors_ok = False, мониторинг открывает инцидент SELECTOR_MISSING.
Текст при этом не теряется: берётся общий экстрактор как запасной путь, а после починки
парсера архив переизвлекается из сохранённого HTML (reparse_archive).

Селекторы — XPath 1.0 (lxml). `blocks` выбирает текстовые блоки тела статьи в порядке
документа; вложенные друг в друга блоки берутся один раз. `drop` удаляется до извлечения
текста (врезки «čtěte také», реклама, подписи к фото, обсуждение).
"""

import datetime
import html as html_lib
import re
from urllib.parse import urlsplit

import lxml.etree
import lxml.html

from apps.documents.models import Document

from ..extract import (
    PRAGUE,
    Extracted,
    _html_lang,
    _json_ld,
    extract_article,
    page_rubric,
    parse_dt,
)
from ..quality import detect_language

ARTICLE_TYPES = {"Article", "NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle",
                 "OpinionNewsArticle", "BlogPosting", "LiveBlogPosting", "WebPage"}  # fmt: skip
ALWAYS_DROP = "//script | //style | //noscript | //template"
MAX_AUTHOR = 300
# Текстовые элементы внутри контейнера тела статьи.
TEXT = "*[self::p or self::h2 or self::h3 or self::h4 or self::li or self::blockquote]"
AUTHOR_PREFIX = re.compile(r"^(?:autor|autoři|autorka|author)\s*:\s*", re.IGNORECASE)
# Měsíce v 2. pádě („4. listopadu 2026“) — pro date_regex se slovním měsícem.
MONTHS = {
    name: i
    for i, name in enumerate(
        ["ledna", "února", "března", "dubna", "května", "června",
         "července", "srpna", "září", "října", "listopadu", "prosince"],
        1,
    )
}  # fmt: skip


def inside(*containers: str) -> str:
    """XPath текстовых блоков внутри контейнеров (в порядке документа)."""
    return " | ".join(f"{c}//{TEXT}" for c in containers)


def clean(text: str) -> str:
    return " ".join(html_lib.unescape(text or "").split())


class SiteParser:
    """Базовый парсер. Подкласс задаёт ключ, домены и селекторы; при необходимости
    переопределяет методы (published, title_of, …)."""

    key = ""  # Source.parser
    name = ""
    version = 1  # повышать при изменении селекторов: видно в Document.meta["parser"]
    hosts: tuple[str, ...] = ()  # домены статей (включая поддомены)

    title = "//h1"  # «jsonld» — headline из JSON-LD, без него первый h1
    perex = ""  # перекс (лид) — первым абзацем текста
    blocks = ""  # обязательный: текстовые блоки тела статьи
    drop = ""  # мусор внутри статьи
    date = ""  # XPath к значению даты; без него — JSON-LD datePublished, meta article:*
    date_regex = ""  # шаблон с группами day, month, year (hour, minute) — для дат в тексте
    local_time = False  # смещение часового пояса на странице неверное: время — пражское
    author = ""  # XPath к именам авторов; без него — JSON-LD author
    rubric = ""  # XPath к рубрике; без него — хлебные крошки и метаданные (page_rubric)
    origin = ""  # XPath к адресам первоисточника (агрегаторы сети Pravda)
    required: tuple[str, ...] = ("title", "text", "published_at")

    # Адрес, куда сайт перенаправляет вместо статьи (страница согласия, пейволл): такую
    # страницу не сохраняем — это ошибка сбора (обычно истекли cookies источника).
    gate = ""  # regex пути

    # Ссылки на статьи со страницы-списка (лента типа «listing»).
    listing_pattern = ""  # regex пути статьи
    listing_xpath = ""  # XPath к href ссылок на статьи

    @property
    def label(self) -> str:
        return f"{self.key}/{self.version}"

    def gated(self, url: str) -> bool:
        return bool(self.gate) and re.search(self.gate, urlsplit(url).path) is not None

    def matches(self, url: str) -> bool:
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    # --- Вспомогательное -------------------------------------------------------------------

    @staticmethod
    def values(root, xpath: str) -> list[str]:
        """Непустые строки по XPath (атрибуты, текст или текст элементов), без повторов."""
        if not xpath:
            return []
        try:
            found = root.xpath(xpath)
        except lxml.etree.XPathError:
            return []
        if not isinstance(found, list):
            found = [found]
        result = []
        for item in found:
            text = clean(item if isinstance(item, str) else item.text_content())
            if text and text not in result:
                result.append(text)
        return result

    def json_ld(self, root) -> dict:
        """Объект статьи из JSON-LD (первый с подходящим @type)."""
        for obj in _json_ld(root):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if ARTICLE_TYPES & {str(t) for t in types} and (
                obj.get("headline") or obj.get("datePublished")
            ):
                return obj
        return {}

    def parse_date(self, value: str) -> datetime.datetime | None:
        if not self.date_regex:
            return self.parse_iso(value)
        match = re.search(self.date_regex, value)
        if match is None:
            return None
        parts = match.groupdict()
        try:
            month = MONTHS.get(parts["month"].lower()) or int(parts["month"])
            return datetime.datetime(
                int(parts["year"]), month, int(parts["day"]),
                int(parts.get("hour") or 0), int(parts.get("minute") or 0), tzinfo=PRAGUE,
            )  # fmt: skip
        except (KeyError, ValueError):
            return None

    def parse_iso(self, value: str) -> datetime.datetime | None:
        parsed = parse_dt(value.strip().replace(" ", "T", 1) if value else value)
        if parsed is not None and self.local_time:
            parsed = parsed.replace(tzinfo=PRAGUE)
        return parsed

    # --- Поля ------------------------------------------------------------------------------

    def title_of(self, root, ld: dict) -> str:
        if self.title == "jsonld":
            headline = clean(str(ld.get("headline") or ""))
            found = [headline] if headline else self.values(root, "//h1")
        else:
            found = self.values(root, self.title)
        return found[0] if found else ""

    def published(self, root, ld: dict, html: str) -> datetime.datetime | None:
        if self.date:
            for value in self.values(root, self.date):
                if (parsed := self.parse_date(value)) is not None:
                    return parsed
            return None
        if self.date_regex:
            return self.parse_date(html)
        return self.parse_meta_date(root, ld)

    def parse_meta_date(self, root, ld: dict) -> datetime.datetime | None:
        """Дата из JSON-LD datePublished, meta article:published_time, itemprop."""
        candidates = [str(ld.get("datePublished") or "")] + self.values(
            root,
            '//meta[@property="article:published_time"]/@content'
            ' | //*[@itemprop="datePublished"]/@content'
            ' | //*[@itemprop="datePublished"]/@datetime',
        )
        for value in candidates:
            if value and (parsed := self.parse_iso(value)) is not None:
                return parsed
        return None

    def authors(self, root, ld: dict) -> str:
        names = (
            [AUTHOR_PREFIX.sub("", n) for n in self.values(root, self.author)]
            if self.author
            else []
        )
        if not self.author:
            people = ld.get("author") or []
            for person in people if isinstance(people, list) else [people]:
                name = person.get("name") if isinstance(person, dict) else person
                if isinstance(name, str) and clean(name) and clean(name) not in names:
                    names.append(clean(name))
        return ", ".join(names)[:MAX_AUTHOR]

    def rubric_of(self, root, html: str, title: str, url: str) -> str:
        if self.rubric:
            return " / ".join(self.values(root, self.rubric))[:200]
        return page_rubric(html, title, url)

    def text_of(self, root) -> str:
        """Текст тела: перекс + блоки, по абзацу на строку. Вызывать после удаления drop."""
        if not self.blocks:
            return ""
        try:
            nodes = root.xpath(self.blocks)
        except lxml.etree.XPathError:
            return ""
        chosen = [n for n in nodes if isinstance(n, lxml.html.HtmlElement)]
        ids = {id(n) for n in chosen}
        parts = []
        for node in chosen:
            if any(id(a) in ids for a in node.iterancestors()):
                continue  # блок внутри уже взятого блока
            text = self.block_text(node)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        return "\n".join(parts)

    def block_text(self, node) -> str:
        return clean(node.text_content())

    def cleanup(self, root) -> None:
        # <br> bez mezer kolem slepí slova („uranuv České“) — zalomení je mezera.
        for br in root.iter("br"):
            br.tail = " " + (br.tail or "")
        for xpath in filter(None, (ALWAYS_DROP, self.drop)):
            try:
                junk = root.xpath(xpath)
            except lxml.etree.XPathError:
                continue
            for node in junk:
                if isinstance(node, lxml.html.HtmlElement) and node.getparent() is not None:
                    node.drop_tree()

    # --- Разбор ----------------------------------------------------------------------------

    def parse(self, html: str, url: str, feed_published_at=None) -> Extracted:
        try:
            root = lxml.html.fromstring(html)
        except (ValueError, lxml.etree.ParserError):
            root = None
        if root is None:
            fallback = extract_article(html, url, feed_published_at)
            fallback.parser, fallback.missing = self.label, ["html"]
            return fallback

        ld = self.json_ld(root)
        missing = []
        title = self.title_of(root, ld)
        published = self.published(root, ld, html)
        author = self.authors(root, ld)
        rubric = self.rubric_of(root, html, title, url)
        origins = self.values(root, self.origin)

        self.cleanup(root)
        perex = next(iter(self.values(root, self.perex)), "")
        text = self.text_of(root)
        if perex and not text.startswith(perex):
            text = f"{perex}\n{text}" if text else perex

        fallback = None
        if not text:
            missing.append("text")
            fallback = extract_article(html, url, feed_published_at)
            text = fallback.text
        if not title:
            missing.append("title")
            title = clean(str(ld.get("headline") or "")) or (fallback.title if fallback else "")

        if published is not None:
            midnight = published.timetz().replace(tzinfo=None) == datetime.time(0, 0)
            if (
                midnight
                and feed_published_at is not None
                and feed_published_at.astimezone(PRAGUE).date() == published.date()
            ):
                published = feed_published_at  # на странице только день — время из ленты
            date_source = Document.DateSource.PAGE
        else:
            missing.append("published_at")
            published = feed_published_at
            date_source = Document.DateSource.FEED if published else Document.DateSource.NONE

        return Extracted(
            title=title,
            text=text,
            published_at=published,
            date_source=date_source,
            language=detect_language(text) or _html_lang(html),
            categories=[],
            canonical_url="",
            rubric=rubric,
            author=author,
            origin_urls=origins,
            parser=self.label,
            missing=[m for m in missing if m in self.required],
        )


def lines_text(node) -> str:
    """Текст блока без абзацев (переносы <br>) — построчно."""
    for br in node.iter("br"):
        br.tail = "\n" + (br.tail or "").lstrip(" ")
    lines = (clean(line) for line in node.text_content().split("\n"))
    return "\n".join(line for line in lines if line)
