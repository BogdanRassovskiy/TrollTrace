"""Protiproud (protiproud.info), vrstva B. Platforma Ghost."""

from . import register
from .base import SiteParser, inside

BODY = '//article[contains(@class, "content")]'


@register
class Protiproud(SiteParser):
    key = "protiproud"
    name = "Protiproud"
    hosts = ("protiproud.info",)
    title = '//h1[contains(@class, "post-hero__title")]'
    blocks = inside(BODY)
    # Medailonek autora uprostřed textu, reklama, upoutávky na pořady (kg-card).
    drop = (
        f'{BODY}//aside | {BODY}//div[contains(@class, "advert")]'
        f' | {BODY}//div[contains(@class, "kg-card")] | {BODY}//figure'
    )
    author = '//meta[@name="author"]/@content'
    rubric = '//meta[@property="article:section"]/@content'
