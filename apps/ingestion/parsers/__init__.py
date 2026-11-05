"""Парсеры веб-источников: по одному на сайт (этап 3.5).

Правило проекта: у каждого веб-источника свой парсер. Источник без зарегистрированного
парсера нельзя включить в сбор (Source.clean, import_sources), а сборщик его не собирает.
Новый сайт = новый модуль в этом пакете + HTML-фикстуры в tests/fixtures/parsers/<ключ>/.

Модули сайтов подключаются автоматически: каждый объявляет подкласс SiteParser с @register.
"""

import importlib
import pkgutil

from ..extract import Extracted, extract_article
from .base import SiteParser

PARSERS: dict[str, SiteParser] = {}


def register(cls: type[SiteParser]) -> type[SiteParser]:
    if not cls.key or cls.key in PARSERS:
        raise ValueError(f"Parser bez klíče nebo s duplicitním klíčem: {cls.key!r}")
    if not cls.blocks:
        raise ValueError(f"Parser {cls.key!r} nemá selektor textu (blocks)")
    PARSERS[cls.key] = cls()
    return cls


def choices() -> list[tuple[str, str]]:
    return sorted(((key, f"{p.name} ({key})") for key, p in PARSERS.items()), key=lambda c: c[1])


def parser_for(source) -> SiteParser | None:
    return PARSERS.get(getattr(source, "parser", "") or "")


def guess(url: str) -> SiteParser | None:
    """Парсер по домену адреса — для подсказки при подключении источника."""
    return next((p for p in PARSERS.values() if p.matches(url)), None)


def extract(source, html: str, url: str, feed_published_at=None) -> Extracted:
    """Извлечение статьи парсером источника; без парсера — общий экстрактор (не веб-сайты
    и старые тесты; веб-источник без парсера сборщик не собирает)."""
    parser = parser_for(source)
    if parser is None:
        config = getattr(source, "collector_config", None) or {}
        return extract_article(html, url, feed_published_at, config.get("text_xpath", ""))
    return parser.parse(html, url, feed_published_at)


def document_meta(extracted: Extracted) -> dict:
    """Что парсер добавляет в Document.meta: версия парсера, ненайденные поля, автор,
    первоисточники. Пустые значения не пишутся."""
    meta = {
        "parser": extracted.parser,
        "selectors_missing": extracted.missing,
        "author": extracted.author,
        "origin_urls": extracted.origin_urls,
    }
    return {k: v for k, v in meta.items() if v}


PARSER_META_KEYS = ("parser", "selectors_missing", "author", "origin_urls")


def merge_meta(meta: dict, extracted: Extracted) -> dict:
    """Метаданные документа с результатом нового извлечения (старые поля парсера заменяются)."""
    kept = {k: v for k, v in (meta or {}).items() if k not in PARSER_META_KEYS}
    return kept | document_meta(extracted)


for _module in pkgutil.iter_modules(__path__):
    if _module.name != "base":
        importlib.import_module(f"{__name__}.{_module.name}")

__all__ = ["PARSERS", "SiteParser", "choices", "document_meta", "extract", "guess",
           "merge_meta", "parser_for", "register"]  # fmt: skip
