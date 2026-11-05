"""ČT24 (ct24.ceskatelevize.cz), vrstva D."""

from . import register
from .base import SiteParser, inside

BODY = '//div[@class="article__container"]'


@register
class CT24(SiteParser):
    key = "ct24"
    name = "ČT24"
    hosts = ("ct24.ceskatelevize.cz",)
    title = "jsonld"  # v h1 je u videí štítek „Video“ před titulkem
    perex = f'{BODY}//p[contains(@class, "fw-bold")]'
    blocks = inside(f'{BODY}//div[contains(@class, "text-content")]')
    drop = (
        f'{BODY}//*[contains(@class, "media-caption") or contains(@class, "link--standard")'
        ' or contains(@class, "tag-list")]'
    )
