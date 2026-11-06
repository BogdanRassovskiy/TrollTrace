"""AC24 (ac24.cz), vrstva B."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "entry-content")]'


@register
class AC24(SiteParser):
    key = "ac24"
    name = "AC24"
    hosts = ("ac24.cz",)
    title = '//h1[contains(@class, "entry-title")]'
    blocks = inside(BODY)
    # Vložené odkazy na jiné články webu, sdílení, reklama, fotky. Vložené tweety (často
    # anglicky) text nenesou, jen ho dokládají — a cizí jazyk by srazil hodnocení dokumentu.
    drop = (
        f'{BODY}//figure | {BODY}//div[contains(@class, "social-sharing")]'
        f' | {BODY}//div[contains(@class, "adswrapper")]'
        f' | {BODY}//blockquote[contains(@class, "twitter-tweet")]'
    )
    author = '//meta[@name="author"]/@content'
