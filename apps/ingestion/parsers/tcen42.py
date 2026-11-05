"""42TCen (42tcen.com), vrstva A."""

from . import register
from .base import SiteParser, inside, lines_text

BODY = '//div[contains(@class, "article__body")]'


@register
class TCen42(SiteParser):
    key = "42tcen"
    name = "42TCen"
    hosts = ("42tcen.com",)
    title = '//h1[contains(@class, "article__title")]'
    perex = f'{BODY}/div[contains(@class, "article__lead")]'
    # Rozhovory: otázky a odpovědi v jednom <p>, oddělené <br><br> — po řádcích.
    blocks = inside(BODY)
    # Na stránce je jen den („05.11.2026“); čas doplní lenta (pravidlo půlnoci).
    date = '//div[contains(@class, "article__date")]'
    date_regex = r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    listing_pattern = r"^/entry/[^/]+$"

    def block_text(self, node) -> str:
        return lines_text(node)
