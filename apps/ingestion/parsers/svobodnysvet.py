"""Svobodný svět (svobodny-svet.cz), vrstva B."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "entry-content")]'


@register
class SvobodnySvet(SiteParser):
    key = "svobodnysvet"
    name = "Svobodný svět"
    hosts = ("svobodny-svet.cz",)
    title = '//h1[contains(@class, "entry-title")]'
    # Autoři vkládají text zkopírovaný z prohlížeče i s obalem reklamy Google — i to je text.
    blocks = inside(BODY) + f' | {BODY}//div[contains(@class, "goog-rentries")]/div'
    # Sdílení, hodnocení, výzvy k podpoře, doporučené články, reklama.
    drop = (
        f'{BODY}//figure | {BODY}//div[contains(@class, "addtoany")]'
        f' | {BODY}//div[contains(@class, "awac-wrapper")]'
        f' | {BODY}//div[contains(@class, "relpost-thumb-wrapper")]'
        f' | {BODY}//aside | {BODY}//div[contains(@class, "ads-")]'
    )
    # JSON-LD uvádí autora jen odkazem; jméno je v meta (často „redakce“).
    author = '//meta[@name="author"]/@content'
