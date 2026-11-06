"""Nová republika (novarepublika.cz), vrstva B."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "entry-content")]'


@register
class NovaRepublika(SiteParser):
    key = "novarepublika"
    name = "Nová republika"
    hosts = ("novarepublika.cz",)
    title = '//h1[contains(@class, "entry-title")]'
    blocks = inside(BODY)
    drop = f'{BODY}//div[contains(@class, "nr-inline-ad")] | {BODY}//figure'
    # Na stránce je jen den („6. listopadu 2026“); čas doplní lenta (pravidlo půlnoci).
    date = '//li[contains(@class, "post-date")]//span[contains(@class, "meta-text")]'
    date_regex = r"(?P<day>\d{1,2})\.\s*(?P<month>[a-zúůéíáčřšž]+)\s+(?P<year>\d{4})"
    author = '//span[contains(@class, "author-text-name")]'
