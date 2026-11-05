"""BIS — aktuality (bis.cz), vrstva I."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "post-detail")]'


@register
class BIS(SiteParser):
    key = "bis"
    name = "BIS — aktuality"
    hosts = ("bis.cz",)
    perex = f'{BODY}//div[@class="post-lead"]'
    blocks = inside(BODY)
    drop = f'{BODY}//*[contains(@class, "post-tags")]'  # štítky „#BIS#2025“
    # Datum je jen v textu stránky („Publikováno: 3. 6. 2025“).
    date_regex = r"Publikováno:\s*(?P<day>\d{1,2})\.\s*(?P<month>\d{1,2})\.\s*(?P<year>\d{4})"
    listing_pattern = r"^/aktuality/[^/]+\.html$"
