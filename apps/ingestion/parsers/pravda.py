"""Pravda Česko (czechia.news-pravda.com), vrstva A — síť Portal Kombat / Pravda.

Pod článkem stránka vypisuje další příspěvky („Top novinky“) se stejnými třídami — text
se bere jen z prvního článku stránky, jinak by se uložil cizí příspěvek (4. 11. 2026).
"""

from . import register
from .base import SiteParser, inside, lines_text

ARTICLE = "(//article)[1]"
TEXT_DIV = f'{ARTICLE}//div[contains(@class, "article__text")]'


@register
class Pravda(SiteParser):
    key = "pravda"
    name = "Pravda Česko (síť Pravda)"
    hosts = ("news-pravda.com",)
    title = (
        f'{ARTICLE}//h1[contains(@class, "article__title")]'
        ' | //h1[contains(@class, "article__title")]'
    )
    # Text bývá v odstavcích <p>, u přeposlaných příspěvků jen s <br> — pak po řádcích.
    blocks = inside(TEXT_DIV) + f" | {TEXT_DIV}[not(.//p)]"
    # Patička převzatá z telegramového kanálu: odkazy „Přístup k diskusi“, „Přihlásit se
    # k odběru kanálu“ — samostatné odstavce s jediným odkazem.
    drop = (
        f'{ARTICLE}//*[contains(@class, "discussion") or contains(@class, "comments")]'
        f" | {TEXT_DIV}//p[a and normalize-space(.) = normalize-space(a)"
        ' and (contains(., "k diskusi") or contains(., "k odběru"))]'
    )
    origin = '//*[contains(@class, "source-link")]/@data-source-url'

    def block_text(self, node) -> str:
        return lines_text(node) if node.tag == "div" else super().block_text(node)
