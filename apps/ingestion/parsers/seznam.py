"""Společná šablona redakčního systému Seznam.cz (Seznam Zprávy, Novinky.cz).

Třídy CSS jsou generované (mění se s každým nasazením webu), stabilní jsou názvy komponent
v atributu data-dot — parser se opírá jen o ně.
"""

from .base import SiteParser, inside

BODY = '//article[@data-dot="ogm-content"]'


class SeznamPlatform(SiteParser):
    title = "jsonld"
    perex = '//p[@data-dot="ogm-article-perex"]'
    blocks = inside(BODY)
    # Odkazy „čtěte také“, karty článků, obrázky, reklama a skrytý nadpis „Článek“.
    drop = (
        f'{BODY}//*[@data-dot="mol-document-links" or @data-dot="mol-document-link"'
        ' or @data-dot="mol-article-card" or self::figure or contains(@class, "js-ad")'
        " or self::h6]"
    )
