"""InfoKurýr (infokuryr.cz/n/), vrstva B."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "entry-content") and contains(@class, "read-details")]'


@register
class InfoKuryr(SiteParser):
    key = "infokuryr"
    name = "InfoKurýr"
    hosts = ("infokuryr.cz",)
    title = '//h1[contains(@class, "entry-title")]'
    blocks = inside(BODY)
    # Podpisy k fotkám, sdílení, štítky, navigace „Continue Reading“.
    drop = (
        f'{BODY}//div[contains(@class, "wp-caption")] | {BODY}//figure'
        f' | {BODY}//div[contains(@class, "heateor_sss")]'
        f' | {BODY}//div[contains(@class, "post-item-metadata")] | {BODY}//nav'
    )
    author = '//meta[@name="author"]/@content'
    rubric = '//meta[@property="article:section"]/@content'
