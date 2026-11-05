"""Blesk.cz (blesk.cz včetně volby.blesk.cz), vrstva D.

Dvě šablony: zpravodajství (div#bbtext, perex .leadsection) a volební web (div.content,
perex .perex).

Datum: u aktualizovaných článků je v meta article:published_time čas aktualizace —
původní čas vydání je v .datetime-start („4. listopadu 2026 • 17:15“).
"""

from . import register
from .base import SiteParser, inside

START = '//div[contains(@class, "datetime-start")]'

BODIES = ('//div[@id="bbtext"]', '//article//div[@class="content"]')


@register
class Blesk(SiteParser):
    key = "blesk"
    name = "Blesk.cz"
    hosts = ("blesk.cz",)
    title = "//h1"
    perex = '//div[@class="leadsection"] | //article//div[@class="perex"]'
    blocks = inside(*BODIES)
    drop = " | ".join(
        f'{b}//*[self::figure or contains(@class, "related") or contains(@class, "gallery")'
        ' or contains(@class, "advert") or contains(@class, "boxik")'
        ' or contains(@class, "article-link")]'  # vložená karta jiného článku
        for b in BODIES
    ) + "".join(  # titulek vloženého videa („VIDEO: …“), není text článku
        f' | {b}//p[starts-with(normalize-space(.), "VIDEO:")]' for b in BODIES
    )
    author = '//meta[@property="article:author"]/@content'
    date_regex = (
        r"(?P<day>\d{1,2})\.\s*(?P<month>\w+)\s+(?P<year>\d{4})"
        r"\s*•\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    )

    def published(self, root, ld, html):
        for value in self.values(root, START):
            if (parsed := self.parse_date(value)) is not None:
                return parsed
        return self.parse_meta_date(root, ld)
