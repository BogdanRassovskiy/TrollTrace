"""RT na ruském (russian.rt.com → russiatoday.ru), vrstva Z. Texty v ruštině."""

from urllib.parse import urlsplit

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "article__text") and contains(@class, "js-mediator-article")]'
# Rubrika podle první části cesty — štítky na stránce jsou jen témata a osoby.
SECTIONS = {
    "world": "В мире",
    "ussr": "Бывший СССР",
    "russia": "Россия",
    "opinion": "Мнения",
    "business": "Экономика",
    "nopolitics": "Без политики",
    "science": "Наука",
    "sport": "Спорт",
}


@register
class Rt(SiteParser):
    key = "rt"
    name = "RT na ruském"
    hosts = ("russiatoday.ru", "russian.rt.com")
    title = '//h1[contains(@class, "article__heading")]'
    # Lead zpráv a článků; komentáře (Мнения) ho nemají.
    perex = '//div[contains(@class, "article__summary")]'
    blocks = inside(BODY)
    # Upoutávky „Также по теме“, fotografie s popiskem, vložená videa, poznámky pod čarou
    # („* … запрещён в России“) a doložka komentářů „Точка зрения автора может не совпадать…“.
    drop = (
        f'{BODY}//div[contains(@class, "read-more")]'
        f' | {BODY}//div[contains(@class, "article__cover")] | {BODY}//figure'
        f' | {BODY}//div[contains(@class, "media")]'
        f' | {BODY}/p[starts-with(normalize-space(.), "*")]'
        f' | {BODY}/p[starts-with(normalize-space(.), "Точка зрения автора")]'
    )
    # Autor článku, nebo autor komentáře (blok autora je na stránce dvakrát).
    author = (
        '//div[contains(@class, "article__author")]'
        ' | //div[contains(@class, "blog-autor__heading")]'
    )
    listing_pattern = r"^/(?:world|ussr|russia|opinion)/(?:(?:news|article)/)?\d+-[a-z0-9-]+$"

    def rubric_of(self, root, html: str, title: str, url: str) -> str:
        section = urlsplit(url).path.strip("/").split("/")[0]
        return SECTIONS.get(section, "")
