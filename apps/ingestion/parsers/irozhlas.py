"""iROZHLAS (irozhlas.cz), vrstva D.

Dvě šablony: běžný článek (div.b-detail) a dlouhé formáty a přepisy podcastů
(article.b-data, např. Vinohradská 12) — tam jsou odstavce bez třídy, za nimi
„Související témata“ a související články.
"""

from . import register
from .base import SiteParser

BODY = '//div[@class="b-detail"]'
DATA = (
    '//article[contains(@class, "b-data")]//div[contains(@class, "b-data__content")]'
    '/div[contains(@class, "row-main")]/div'
)


@register
class IRozhlas(SiteParser):
    key = "irozhlas"
    name = "iROZHLAS"
    hosts = ("irozhlas.cz",)
    title = "jsonld"
    perex = (
        f'{BODY}/header//p[contains(@class, "text-bold--m")]'
        f' | {DATA}/p[contains(@class, "text-bold--m")]'
    )
    # Text jsou přímé odstavce detailu; hlavička (perex, místo a čas) a patička (autor) zvlášť.
    blocks = (
        f"{BODY}/p[not(contains(@class, 'meta'))] | {BODY}/*[self::h2 or self::h3]"
        f" | {BODY}/*[self::ul or self::ol]/li"
        f" | {BODY}/blockquote[not(contains(@class, 'twitter'))]"
        f" | {DATA}/p[not(@class)] | {DATA}/h2[not(@class)]"
    )
    drop = (
        " | ".join(
            f'{b}//*[contains(@class, "b-inline") or self::figure'
            ' or contains(@class, "twitter-tweet") or contains(@class, "instagram-media")]'
            for b in (BODY, DATA)
        )
        + f' | {DATA}/p[starts-with(normalize-space(.), "Související témata")]'
    )
