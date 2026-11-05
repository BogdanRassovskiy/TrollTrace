"""ČTK — České noviny (ceskenoviny.cz), vrstva D."""

from . import register
from .base import SiteParser, inside

BODY = '//div[@id="articlebody"]'


@register
class CeskeNoviny(SiteParser):
    key = "ctk"
    name = "ČTK — České noviny"
    hosts = ("ceskenoviny.cz",)
    title = "jsonld"  # první h1 stránky je logo „České noviny“
    blocks = inside(BODY)
    drop = (
        f'{BODY}//*[self::figure or contains(@class, "tags") or contains(@class, "related")'
        ' or contains(@class, "box-article-footer")'  # autor, sdílení, tisk
        ' or contains(@class, "infobox")'  # odkaz na fotogalerii
        ' or contains(@class, "crossroad")]'  # související články
    )
    # Datum z řádku „Vydáno: 4.11.2026, 14:15“ (pražský čas). JSON-LD nelze použít: posun
    # je +02:00 i v zimě a při pozdějším stažení stránky obsahuje čas stažení, ne vydání.
    date = '//div[contains(@class, "box-article-info")]'
    date_regex = (
        r"Vydáno:\s*(?P<day>\d{1,2})\.\s*(?P<month>\d{1,2})\.\s*(?P<year>\d{4}),?"
        r"\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    )
